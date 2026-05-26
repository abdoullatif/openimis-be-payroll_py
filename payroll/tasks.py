import logging
from celery import shared_task

from core.models import User
from payroll.models import Payroll, BenefitConsumptionStatus
from payroll.reconciliation_benefit_utils import apply_gateway_pull_result, save_benefit_if_dirty
from payroll.reconciliation_lock import (
    ensure_payroll_reconciliation_not_locked,
    set_payment_in_progress,
    set_reconciliation_in_progress,
)
from payroll.payroll_reconciliation_status import record_reconciliation_run_summary
from payroll.reconciliation_payload import normalize_reconciliation_gateway_result
from payroll.strategies import StrategyOnlinePayment
from payroll.payments_registry import PaymentMethodStorage

logger = logging.getLogger(__name__)


@shared_task(name="payroll.tasks.sync_payroll_creation_to_opensearch_task")
def sync_payroll_creation_to_opensearch_task(payroll_id, client_mutation_id=None, username=None):
    logger.info(
        "[Celery] sync_payroll_creation_to_opensearch_task payroll_id=%s mutation=%s",
        payroll_id,
        client_mutation_id,
    )
    from payroll.models import Payroll
    from payroll.opensearch_indexing_progress import (
        complete_payroll_opensearch_indexing,
        fail_payroll_opensearch_indexing,
    )
    from payroll.opensearch_payroll_status_sync import sync_payroll_creation_to_opensearch

    try:
        payroll = Payroll.objects.select_related(
            "payment_plan",
            "payment_plan__benefit_plan_type",
            "payment_cycle",
        ).get(id=payroll_id, is_deleted=False)
        from payroll.opensearch_indexing_progress import start_payroll_opensearch_indexing

        start_payroll_opensearch_indexing(
            payroll_id,
            client_mutation_id=client_mutation_id,
            username=username,
            persist_to_db=True,
        )
        sync_payroll_creation_to_opensearch(payroll)
        complete_payroll_opensearch_indexing(
            payroll_id,
            client_mutation_id=client_mutation_id,
            username=username,
        )
    except Exception as exc:
        logger.exception(
            "[Celery] OpenSearch creation sync failed payroll_id=%s: %s",
            payroll_id,
            exc,
        )
        fail_payroll_opensearch_indexing(
            payroll_id,
            exc,
            client_mutation_id=client_mutation_id,
            username=username,
        )
        raise


@shared_task
def send_requests_to_gateway_payment(payroll_id, user_id):
    logger.info("[Celery] send_requests_to_gateway_payment received payroll_id=%s user_id=%s", payroll_id, user_id)
    payroll = Payroll.objects.select_related("payment_plan").get(id=payroll_id)
    user = User.objects.get(id=user_id)
    username = getattr(user, "login_name", None) or user.username
    try:
        strategy = PaymentMethodStorage.get_chosen_payment_method(payroll.payment_method)
        if strategy:
            strategy.initialize_payment_gateway()
            strategy.make_payment_for_payroll(payroll, user)
        else:
            logger.warning(
                "[Celery] No payment strategy for payroll_id=%s payment_method=%s",
                payroll_id, getattr(payroll, 'payment_method', None),
            )
    except Exception:
        from payroll.payment_progress import fail_payroll_payment_progress

        fail_payroll_payment_progress(str(payroll_id), "Payment job failed")
        raise
    finally:
        payroll.refresh_from_db()
        set_payment_in_progress(payroll, user, False)
        from payroll.payment_progress import get_payroll_payment_progress, persist_payment_progress_to_payroll

        progress = get_payroll_payment_progress(str(payroll_id))
        if progress:
            persist_payment_progress_to_payroll(payroll, progress, username)


@shared_task
def send_request_to_reconcile(payroll_id, user_id):
    payroll = Payroll.objects.select_related("payment_plan").get(id=payroll_id)
    user = User.objects.get(id=user_id)
    username = getattr(user, "login_name", None) or user.username
    payroll_id_str = str(payroll_id)
    try:
        ensure_payroll_reconciliation_not_locked(payroll)
        strategy = StrategyOnlinePayment
        strategy.initialize_payment_gateway()
        benefits = list(
            strategy.get_benefits_attached_to_payroll(
                payroll, BenefitConsumptionStatus.APPROVE_FOR_PAYMENT
            )
        )
        from payroll.reconciliation_progress import (
            complete_payroll_reconciliation_progress,
            fail_payroll_reconciliation_progress,
            start_payroll_reconciliation_progress,
            update_payroll_reconciliation_progress,
        )

        total = len(benefits)
        start_payroll_reconciliation_progress(payroll_id_str, total, username=username)
        payment_gateway_connector = strategy.PAYMENT_GATEWAY
        projet, campagne = strategy._get_project_and_campaign(payroll)
        benefits_to_reconcile = []
        operator_receipts = {}
        success_count = 0
        rejected_count = 0
        processed = 0
        for benefit in benefits:
            processed += 1
            code_menage = strategy._get_code_menage(benefit)
            gateway_result = normalize_reconciliation_gateway_result(
                payment_gateway_connector.reconcile(
                    benefit.code, benefit.amount,
                    projet=projet, campagne=campagne, code_menage=code_menage
                )
            )
            if apply_gateway_pull_result(benefit, gateway_result):
                save_benefit_if_dirty(benefit, username)
            if gateway_result["success"]:
                success_count += 1
                if gateway_result.get("receipt"):
                    operator_receipts[benefit.code] = gateway_result["receipt"]
                benefits_to_reconcile.append(benefit)
            else:
                rejected_count += 1
                logger.info("Payment for benefit (%s) was rejected.", benefit.code)
            update_payroll_reconciliation_progress(
                payroll_id_str,
                processed,
                total,
                success_count=success_count,
                rejected_count=rejected_count,
                username=username,
            )
        if benefits_to_reconcile:
            strategy.reconcile_benefit_consumption(
                benefits_to_reconcile, user, operator_receipts=operator_receipts
            )
        record_reconciliation_run_summary(
            payroll, user, success_count=success_count, rejected_count=rejected_count
        )
        complete_payroll_reconciliation_progress(
            payroll_id_str,
            processed,
            total,
            success_count=success_count,
            rejected_count=rejected_count,
            username=username,
        )
    except Exception:
        from payroll.reconciliation_progress import fail_payroll_reconciliation_progress

        fail_payroll_reconciliation_progress(
            payroll_id_str, "Reconciliation job failed", username=username
        )
        raise
    finally:
        payroll.refresh_from_db()
        set_reconciliation_in_progress(payroll, user, False)
        from payroll.reconciliation_progress import (
            get_payroll_reconciliation_progress,
            persist_reconciliation_progress_to_payroll,
        )

        progress = get_payroll_reconciliation_progress(payroll_id_str)
        if progress:
            persist_reconciliation_progress_to_payroll(payroll, progress, username)
