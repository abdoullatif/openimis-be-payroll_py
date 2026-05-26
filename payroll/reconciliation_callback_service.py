import logging
from decimal import Decimal, InvalidOperation

from core.models import User
from payroll.models import BenefitConsumption, BenefitConsumptionStatus
from payroll.payments_registry import PaymentMethodStorage
from payroll.reconciliation_benefit_utils import merge_benefit_json_ext, save_benefit_if_dirty
from payroll.reconciliation_lock import ensure_payroll_reconciliation_not_locked
from payroll.reconciliation_payload import parse_reconciliation_callback_payload
from payroll.strategies.strategy_online_payment import StrategyOnlinePayment

logger = logging.getLogger(__name__)


class ReconciliationCallbackService:
    """
    Traite une notification push de réconciliation (une facture / benefit).
    L'opérateur envoie identification + success (+ receipt si succès).
    """

    def __init__(self, user):
        self.user = user

    @classmethod
    def get_callback_user(cls):
        username = PayrollConfig.reconciliation_callback_username
        if not username:
            raise ValueError("reconciliation_callback_username is not configured")
        user = User.objects.filter(username=username, validity_to__isnull=True).first()
        if not user:
            raise ValueError(f"Callback user '{username}' not found")
        return user

    def process(self, data):
        payload = parse_reconciliation_callback_payload(data)
        benefit = BenefitConsumption.objects.filter(
            code=payload["invoiceId"],
            is_deleted=False,
        ).select_related("individual").first()
        if not benefit:
            raise ValueError(f"Benefit consumption not found for invoiceId={payload['invoiceId']}")

        payroll_link = benefit.payrollbenefitconsumption_set.filter(
            is_deleted=False,
            payroll__is_deleted=False,
        ).select_related("payroll", "payroll__payment_plan").first()
        if not payroll_link:
            raise ValueError(f"No payroll linked to invoiceId={payload['invoiceId']}")

        payroll = payroll_link.payroll
        self._ensure_online_payment_payroll(payroll)
        ensure_payroll_reconciliation_not_locked(payroll)
        self._validate_amount(benefit, payload["amount"])
        self._validate_context(benefit, payroll, payload)

        if benefit.status == BenefitConsumptionStatus.RECONCILED:
            return self._build_response(
                benefit, payroll, payload,
                already_reconciled=True,
                recorded=False,
            )

        if benefit.status != BenefitConsumptionStatus.APPROVE_FOR_PAYMENT:
            raise ValueError(
                f"Benefit {benefit.code} cannot be reconciled (status={benefit.status})"
            )

        if not payload["success"]:
            return self._record_reconciliation_failure(benefit, payroll, payload)

        return self._record_reconciliation_success(benefit, payroll, payload)

    def _record_reconciliation_failure(self, benefit, payroll, payload):
        username = getattr(self.user, "login_name", None) or self.user.username
        if merge_benefit_json_ext(benefit, {
            "gateway_reconciliation_success": False,
            "reconciliation_source": "operator_callback",
            "reconciliation_callback_payload": payload,
            "output_gateway": False,
        }):
            save_benefit_if_dirty(benefit, username)

        logger.info(
            "[Reconciliation callback] failure invoiceId=%s payroll_id=%s",
            benefit.code,
            payroll.id,
        )
        return self._build_response(
            benefit, payroll, payload,
            already_reconciled=False,
            recorded=True,
        )

    def _record_reconciliation_success(self, benefit, payroll, payload):
        strategy = PaymentMethodStorage.get_chosen_payment_method(payroll.payment_method)
        if not strategy:
            raise ValueError(f"No payment strategy for payroll {payroll.id}")

        operator_receipt = payload["receipt"]
        merge_benefit_json_ext(benefit, {
            "gateway_reconciliation_success": True,
            "reconciliation_source": "operator_callback",
            "reconciliation_callback_payload": payload,
            "operator_transaction_id": operator_receipt,
            "output_gateway": True,
        })

        strategy.reconcile_benefit_consumption(
            [benefit],
            self.user,
            operator_receipts={benefit.code: operator_receipt},
        )
        benefit.refresh_from_db()

        logger.info(
            "[Reconciliation callback] success invoiceId=%s payroll_id=%s receipt=%s",
            benefit.code,
            payroll.id,
            benefit.receipt,
        )
        return self._build_response(
            benefit, payroll, payload,
            already_reconciled=False,
            recorded=True,
        )

    def _ensure_online_payment_payroll(self, payroll):
        if payroll.payment_method != StrategyOnlinePayment.__name__:
            raise ValueError("Payroll payment method must be StrategyOnlinePayment")

    def _validate_amount(self, benefit, payload_amount):
        try:
            benefit_amount = Decimal(str(benefit.amount))
            received_amount = Decimal(str(payload_amount))
        except (InvalidOperation, TypeError) as exc:
            raise ValueError("Invalid amount format") from exc
        if benefit_amount != received_amount:
            raise ValueError(
                f"Amount mismatch for {benefit.code}: expected {benefit_amount}, got {received_amount}"
            )

    def _validate_context(self, benefit, payroll, payload):
        projet, campagne = StrategyOnlinePayment._get_project_and_campaign(payroll)
        if payload["projet"] and projet and str(projet) != payload["projet"]:
            raise ValueError("projet does not match payroll context")
        if payload["campagne"] and campagne and str(campagne) != payload["campagne"]:
            raise ValueError("campagne does not match payroll context")
        code_menage = StrategyOnlinePayment._get_code_menage(benefit)
        if payload["codeMenage"] and code_menage and str(code_menage) != payload["codeMenage"]:
            raise ValueError("codeMenage does not match benefit context")

    def _build_response(self, benefit, payroll, payload, already_reconciled, recorded):
        operator_success = payload.get("success", True)
        if already_reconciled:
            return {
                "success": True,
                "invoiceId": benefit.code,
                "amount": str(benefit.amount),
                "payrollId": str(payroll.id),
                "status": benefit.status,
                "receipt": benefit.receipt,
                "alreadyReconciled": True,
                "recorded": False,
                "message": "Benefit was already reconciled",
            }
        if not operator_success:
            return {
                "success": False,
                "invoiceId": benefit.code,
                "amount": str(benefit.amount),
                "payrollId": str(payroll.id),
                "status": benefit.status,
                "receipt": None,
                "alreadyReconciled": False,
                "recorded": recorded,
                "message": "Reconciliation failure recorded",
            }
        return {
            "success": True,
            "invoiceId": benefit.code,
            "amount": str(benefit.amount),
            "payrollId": str(payroll.id),
            "status": benefit.status,
            "receipt": benefit.receipt,
            "alreadyReconciled": False,
            "recorded": recorded,
            "message": "Reconciliation recorded successfully",
        }
