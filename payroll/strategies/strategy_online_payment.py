import logging

from django.db.models import Q, Sum
from django.db import transaction

from core.signals import register_service_signal
from payroll.strategies.strategy_of_payments_interface import StrategyOfPaymentInterface
from payroll.utils import CodeGenerator

logger = logging.getLogger(__name__)


class StrategyOnlinePayment(StrategyOfPaymentInterface):
    WORKFLOW_NAME = "payment-adaptor"
    WORKFLOW_GROUP = "openimis-coremis-payment-adaptor"
    PAYMENT_GATEWAY = None

    @classmethod
    def initialize_payment_gateway(cls):
        from payroll.payment_gateway import PaymentGatewayConfig
        gateway_config = PaymentGatewayConfig()
        payment_gateway_connector_class = gateway_config.get_payment_gateway_connector()
        cls.PAYMENT_GATEWAY = payment_gateway_connector_class()

    @classmethod
    def accept_payroll(cls, payroll, user, **kwargs):
        cls._process_accepted_payroll(payroll, user, **kwargs)

    @classmethod
    def make_payment_for_payroll(cls, payroll, user, **kwargs):
        cls._send_payment_data_to_gateway(payroll, user)

    @classmethod
    def acknowledge_of_reponse_view(cls, payroll, response_from_gateway, user, rejected_bills):
        # save response coming from payment gateway in json_ext
        cls._save_payroll_data(payroll, user, response_from_gateway)

    @classmethod
    @transaction.atomic
    def reconcile_payroll(cls, payroll, user):
        from payroll.tasks import send_request_to_reconcile
        send_request_to_reconcile.delay(payroll.id, user.id)

    @classmethod
    def get_benefits_attached_to_payroll(cls, payroll, status):
        from payroll.models import BenefitConsumption
        filters = Q(
            payrollbenefitconsumption__payroll_id=payroll.id,
            is_deleted=False,
            status=status,
            payrollbenefitconsumption__is_deleted=False,
            payrollbenefitconsumption__payroll__is_deleted=False,
        )
        benefits = BenefitConsumption.objects.filter(filters).select_related("individual")
        return benefits

    @classmethod
    def approve_for_payment_benefit_consumption(cls, benefits, user):
        from payroll.models import BenefitConsumptionStatus
        from payroll.opensearch_payroll_status_sync import benefit_status_only_save

        for benefit in benefits:
            try:
                benefit.status = BenefitConsumptionStatus.APPROVE_FOR_PAYMENT
                benefit_status_only_save(benefit, user.login_name)
            except Exception as e:
                logger.debug(f"Failed to approve benefit consumption {benefit.code}: {str(e)}")

    @classmethod
    def reconcile_benefit_consumption(cls, benefits, user, operator_receipts=None):
        from payroll.models import BenefitConsumptionStatus, PayrollBenefitConsumption
        from payroll.apps import PayrollConfig
        from payroll.opensearch_payroll_status_sync import (
            benefit_status_only_save,
            skip_heavy_opensearch_reindex,
            sync_benefit_status_batch_to_opensearch,
        )
        from invoice.models import Bill

        operator_receipts = operator_receipts or {}
        benefit_ids_for_os = []
        payroll_id = None
        os_batch_size = 500

        for benefit in benefits:
            try:
                receipt = (
                    operator_receipts.get(str(benefit.id))
                    or operator_receipts.get(benefit.code)
                )
                if not receipt:
                    receipt = CodeGenerator.generate_unique_code(
                        'payroll',
                        'BenefitConsumption',
                        'receipt',
                        PayrollConfig.receipt_length,
                    )
                benefit.receipt = receipt
                benefit.status = BenefitConsumptionStatus.RECONCILED
                benefit_status_only_save(benefit, user.login_name)
                benefit_ids_for_os.append(benefit.id)
                if payroll_id is None:
                    payroll_id = (
                        PayrollBenefitConsumption.objects.filter(
                            benefit_id=benefit.id,
                            is_deleted=False,
                        )
                        .values_list("payroll_id", flat=True)
                        .first()
                    )
                bill = Bill.objects.filter(
                    benefitattachment__benefit=benefit,
                    is_deleted=False
                ).first()
                if bill:
                    with skip_heavy_opensearch_reindex():
                        cls._create_bill_payment_for_paid_bill(benefit, bill, user)
            except Exception as e:
                logger.debug(f"Failed to approve benefit consumption {benefit.code}: {str(e)}")

        if payroll_id and benefit_ids_for_os:
            for offset in range(0, len(benefit_ids_for_os), os_batch_size):
                sync_benefit_status_batch_to_opensearch(
                    payroll_id,
                    benefit_ids_for_os[offset : offset + os_batch_size],
                    BenefitConsumptionStatus.RECONCILED,
                )

    @classmethod
    def _create_bill_payment_for_paid_bill(cls, benefit, bill, user):
        from core import datetime
        from django.contrib.contenttypes.models import ContentType
        from invoice.models import Bill, DetailPaymentInvoice, PaymentInvoice
        from invoice.services import PaymentInvoiceService
        current_date = datetime.date.today()
        bill.status = Bill.Status.RECONCILIATED
        bill.date_payed = current_date
        bill.save(username=user.login_name)
        # Create a BillPayment object for the 'Paid' bill
        bill_payment = {
            "code_tp": bill.code_tp,
            "code_ext": bill.code_ext,
            "code_receipt": bill.code,
            "label": bill.terms,
            'reconciliation_status': PaymentInvoice.ReconciliationStatus.RECONCILIATED,
            "fees": 0.0,
            "amount_received": bill.amount_total,
            "date_payment": current_date,
            'payment_origin': "online payment",
            'payer_ref': 'payment reference',
            'payer_name': 'payer name',
            "json_ext": {}
        }

        bill_payment_details = {
            'subject_type': ContentType.objects.get_for_model(bill),
            'subject': bill,
            'status': DetailPaymentInvoice.DetailPaymentStatus.ACCEPTED,
            'fees': 0.0,
            'amount': bill.amount_total,
            'reconcilation_id': benefit.receipt,
            'reconcilation_date': current_date,
        }
        bill_payment_details = DetailPaymentInvoice(**bill_payment_details)
        payment_service = PaymentInvoiceService(user)
        payment_service.create_with_detail(bill_payment, bill_payment_details)

    @classmethod
    def _get_payroll_bills_amount(cls, payroll):
        from payroll.models import Payroll
        payroll_with_benefit_sum = Payroll.objects.filter(id=payroll.id).annotate(
            total_benefit_amount=Sum('payrollbenefitconsumption__benefit__amount')
        ).first()
        return payroll_with_benefit_sum.total_benefit_amount

    @classmethod
    def _get_benefits_to_string(cls, benefits):
        benefits_uuids = [str(benefit.id) for benefit in benefits]
        benefits_uuids_string = ",".join(benefits_uuids)
        return benefits_uuids_string

    @classmethod
    def _send_payment_data_to_gateway(cls, payroll, user):
        """
        Exécuté uniquement dans le worker Celery (send_requests_to_gateway_payment).
        Appels passerelle + save DB avec skip OpenSearch ; sync léger par lots.
        """
        from payroll.models import BenefitConsumptionStatus
        from payroll.payment_progress import (
            complete_payroll_payment_progress,
            fail_payroll_payment_progress,
            start_payroll_payment_progress,
            update_payroll_payment_progress,
        )
        from payroll.opensearch_payroll_status_sync import (
            benefit_status_only_save,
            sync_benefit_status_batch_to_opensearch,
        )

        benefits = list(
            cls.get_benefits_attached_to_payroll(payroll, BenefitConsumptionStatus.ACCEPTED)
        )
        total = len(benefits)
        payroll_id = str(payroll.id)
        username = user.login_name
        start_payroll_payment_progress(payroll_id, total, username=username)

        payment_gateway_connector = cls.PAYMENT_GATEWAY
        projet, campagne = cls._get_project_and_campaign(payroll)
        pending_os_sync_ids = []
        success_count = 0
        rejected_count = 0
        os_batch_size = 500

        try:
            for index, benefit in enumerate(benefits, start=1):
                code_menage = cls._get_code_menage(benefit)
                if payment_gateway_connector.send_payment(
                    benefit.code,
                    benefit.amount,
                    projet=projet,
                    campagne=campagne,
                    code_menage=code_menage,
                ):
                    benefit.status = BenefitConsumptionStatus.APPROVE_FOR_PAYMENT
                    benefit_status_only_save(benefit, user.login_name)
                    pending_os_sync_ids.append(benefit.id)
                    success_count += 1
                else:
                    rejected_count += 1
                    logger.info("Payment for benefit (%s) was rejected.", benefit.code)

                update_payroll_payment_progress(
                    payroll_id,
                    index,
                    total,
                    success_count=success_count,
                    rejected_count=rejected_count,
                    username=username,
                )

                if len(pending_os_sync_ids) >= os_batch_size:
                    sync_benefit_status_batch_to_opensearch(
                        payroll_id,
                        pending_os_sync_ids,
                        BenefitConsumptionStatus.APPROVE_FOR_PAYMENT,
                    )
                    pending_os_sync_ids = []

            if pending_os_sync_ids:
                sync_benefit_status_batch_to_opensearch(
                    payroll_id,
                    pending_os_sync_ids,
                    BenefitConsumptionStatus.APPROVE_FOR_PAYMENT,
                )

            complete_payroll_payment_progress(
                payroll_id,
                total,
                total,
                success_count=success_count,
                rejected_count=rejected_count,
                username=username,
            )
        except Exception as exc:
            fail_payroll_payment_progress(payroll_id, exc, username=username)
            raise

    @classmethod
    def _process_accepted_payroll(cls, payroll, user, **kwargs):
        from payroll.models import PayrollStatus
        cls.change_status_of_payroll(
            payroll, PayrollStatus.APPROVE_FOR_PAYMENT, user, opensearch_status_only=True
        )

    @classmethod
    def _get_code_menage(cls, benefit):
        """code_menage depuis extra_info du benefit ou json_ext de l'individual"""
        extra_info = (benefit.json_ext or {}).get("extra_info") or {}
        code_menage = extra_info.get("code_menage")
        if code_menage is None and benefit.individual and benefit.individual.json_ext:
            code_menage = (benefit.individual.json_ext or {}).get("code_menage")
        return code_menage

    @classmethod
    def _get_payroll_campaign(cls, payroll):
        """Nom de la paie (Payroll.name) envoyé à la passerelle comme campagne."""
        if not payroll:
            return None
        name = getattr(payroll, "name", None)
        return str(name).strip() if name else None

    @classmethod
    def _get_project_and_campaign(cls, payroll):
        """projet = BenefitPlan.code, campagne = Payroll.name."""
        projet = campagne = None
        if payroll:
            campagne = cls._get_payroll_campaign(payroll)
        payment_plan = getattr(payroll, "payment_plan", None)
        if payment_plan:
            benefit_plan = getattr(payment_plan, "benefit_plan", None)
            if benefit_plan:
                projet = getattr(benefit_plan, "code", None)
        return projet, campagne

    @classmethod
    def _save_payroll_data(cls, payroll, user, response_from_gateway):
        json_ext = payroll.json_ext if payroll.json_ext else {}
        json_ext['response_from_gateway'] = response_from_gateway
        payroll.json_ext = json_ext
        payroll.save(username=user.username)
        cls._create_payroll_reconcilation_task(payroll, user)

    @classmethod
    @register_service_signal('online_payments.create_task')
    def _create_payroll_reconcilation_task(cls, payroll, user):
        from payroll.apps import PayrollConfig
        from tasks_management.services import TaskService
        from tasks_management.apps import TasksManagementConfig
        from tasks_management.models import Task
        TaskService(user).create({
            'source': 'payroll_reconciliation',
            'entity': payroll,
            'status': Task.Status.RECEIVED,
            'executor_action_event': TasksManagementConfig.default_executor_event,
            'business_event': PayrollConfig.payroll_reconciliation_event,
        })
