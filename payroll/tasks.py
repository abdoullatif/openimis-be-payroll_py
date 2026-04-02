import logging
from celery import shared_task

from core.models import User
from payroll.models import Payroll, PayrollStatus, BenefitConsumptionStatus
from payroll.strategies import StrategyOnlinePayment
from payroll.payments_registry import PaymentMethodStorage

logger = logging.getLogger(__name__)


@shared_task
def send_requests_to_gateway_payment(payroll_id, user_id):
    logger.info("[Celery] send_requests_to_gateway_payment received payroll_id=%s user_id=%s", payroll_id, user_id)
    payroll = Payroll.objects.select_related("payment_plan").get(id=payroll_id)
    strategy = PaymentMethodStorage.get_chosen_payment_method(payroll.payment_method)
    if strategy:
        user = User.objects.get(id=user_id)
        strategy.initialize_payment_gateway()
        strategy.make_payment_for_payroll(payroll, user)
    else:
        logger.warning("[Celery] No payment strategy for payroll_id=%s payment_method=%s", payroll_id, getattr(payroll, 'payment_method', None))


@shared_task
def send_request_to_reconcile(payroll_id, user_id):
    payroll = Payroll.objects.select_related("payment_plan").get(id=payroll_id)
    user = User.objects.get(id=user_id)
    strategy = StrategyOnlinePayment
    strategy.initialize_payment_gateway()
    strategy.change_status_of_payroll(payroll, PayrollStatus.RECONCILED, user)
    benefits = strategy.get_benefits_attached_to_payroll(payroll, BenefitConsumptionStatus.APPROVE_FOR_PAYMENT)
    payment_gateway_connector = strategy.PAYMENT_GATEWAY
    projet, campagne = strategy._get_project_and_campaign(payroll)
    benefits_to_reconcile = []
    for benefit in benefits:
        code_menage = strategy._get_code_menage(benefit)
        is_reconciled = payment_gateway_connector.reconcile(
            benefit.code, benefit.amount,
            projet=projet, campagne=campagne, code_menage=code_menage
        )
        # Initialize json_ext if it is None
        if benefit.json_ext is None:
            benefit.json_ext = {}
        if is_reconciled:
            new_json_ext = benefit.json_ext.copy() if benefit.json_ext else {}
            new_json_ext['output_gateway'] = is_reconciled
            new_json_ext['gateway_reconciliation_success'] = True
            benefit.json_ext = {**benefit.json_ext, **new_json_ext}
            benefits_to_reconcile.append(benefit)
        else:
            # Handle the case where a benefit payment is rejected
            new_json_ext = benefit.json_ext.copy() if benefit.json_ext else {}
            new_json_ext['output_gateway'] = is_reconciled
            new_json_ext['gateway_reconciliation_success'] = False
            benefit.json_ext = {**benefit.json_ext, **new_json_ext}
            benefit.save(username=user.login_name)
            logger.info(f"Payment for benefit ({benefit.code}) was rejected.")
    if benefits_to_reconcile:
        strategy.reconcile_benefit_consumption(benefits_to_reconcile, user)
