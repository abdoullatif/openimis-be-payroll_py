import logging
import time
import pandas as pd
from io import BytesIO
import unicodedata

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils.translation import gettext as _

from core import datetime
from core.custom_filters import CustomFilterWizardStorage
from core.custom_filters.filter_condition_utils import extract_custom_filters_from_json_ext
from core.models import InteractiveUser
from core.services import BaseService
from core.signals import register_service_signal
from invoice.models import Bill, PaymentInvoice, DetailPaymentInvoice
from invoice.services import PaymentInvoiceService
from payment_cycle.models import PaymentCycle
from payroll.apps import PayrollConfig
from payroll.models import (
    PaymentPoint,
    Payroll,
    PayrollBenefitConsumption,
    BenefitConsumption,
    BenefitAttachment,
    BenefitConsumptionStatus
)
from payroll.tasks import send_requests_to_gateway_payment, send_request_to_reconcile
from payroll.reconciliation_lock import (
    ensure_payroll_reconciliation_not_locked,
    is_payment_in_progress,
    is_reconciliation_in_progress,
    set_payment_in_progress,
    set_reconciliation_in_progress,
)
from payroll.strategies.strategy_online_payment import StrategyOnlinePayment
from payroll.payments_registry import PaymentMethodStorage
from payroll.validation import PaymentPointValidation, PayrollValidation, BenefitConsumptionValidation
from payroll.strategies import StrategyOfPaymentInterface
from calculation.services import get_calculation_object
from core.services.utils import output_exception, check_authentication
from contribution_plan.models import PaymentPlan
from social_protection.models import Beneficiary, BeneficiaryStatus
from tasks_management.apps import TasksManagementConfig
from tasks_management.models import Task
from tasks_management.services import TaskService, _get_std_task_data_payload
from payroll.models import PaymentReport
from payroll.payroll_accept_task_recap import (
    build_payroll_accept_task_display,
    build_payroll_accept_task_payload,
)
from payroll.payroll_reconciliation_status import (
    build_payroll_reconciliation_task_incoming_data,
    format_reconciliation_recap_text,
    resolve_reconciliation_recap_from_task_data,
)

logger = logging.getLogger(__name__)


class PaymentPointService(BaseService):
    OBJECT_TYPE = PaymentPoint

    def __init__(self, user, validation_class=PaymentPointValidation):
        super().__init__(user, validation_class)

    @register_service_signal('payment_point_service.create')
    def create(self, obj_data):
        return super().create(obj_data)

    @register_service_signal('payment_point_service.update')
    def update(self, obj_data):
        return super().update(obj_data)

    @register_service_signal('payment_point_service.delete')
    def delete(self, obj_data):
        return super().delete(obj_data)


class PayrollService(BaseService):
    OBJECT_TYPE = Payroll

    def __init__(self, user, validation_class=PayrollValidation):
        super().__init__(user, validation_class)

    @check_authentication
    @register_service_signal('payroll_service.create')
    def create(self, obj_data):
        client_mutation_id = obj_data.get("client_mutation_id")
        payroll_id_for_cleanup = None
        dict_representation = None
        try:
            from payroll.opensearch_payroll_status_sync import skip_heavy_opensearch_reindex

            with skip_heavy_opensearch_reindex():
                obj_data = self._adjust_create_payload(obj_data)
                client_mutation_id = obj_data.pop("client_mutation_id", None) or client_mutation_id
                if client_mutation_id:
                    from payroll.creation_progress import begin_payroll_creation_progress

                    begin_payroll_creation_progress(client_mutation_id)

                with transaction.atomic():
                    from_failed_invoices_payroll_id = obj_data.pop("from_failed_invoices_payroll_id", None)
                    payment_plan = self._get_payment_plan(obj_data)
                    payment_cycle = self._get_payment_cycle(obj_data)
                    date_valid_from, date_valid_to = self._get_dates_parameter(obj_data)
                    payroll, dict_representation = self._save_payroll(obj_data)
                    payroll_id_for_cleanup = str(payroll.id)

                    total_beneficiaries = 0
                    beneficiaries_queryset = None
                    if not bool(from_failed_invoices_payroll_id):
                        beneficiaries_queryset = self._select_beneficiary_based_on_criteria(
                            obj_data, payment_plan
                        )
                        total_beneficiaries = beneficiaries_queryset.count()

                    if client_mutation_id:
                        from payroll.creation_progress import register_payroll_creation_progress

                        register_payroll_creation_progress(
                            client_mutation_id,
                            payroll_id_for_cleanup,
                            total_beneficiaries,
                        )

                    if not bool(from_failed_invoices_payroll_id):
                        self._generate_benefits(
                            payment_plan,
                            beneficiaries_queryset,
                            date_valid_from,
                            date_valid_to,
                            payroll,
                            payment_cycle,
                            client_mutation_id=client_mutation_id,
                        )
                    else:
                        self._move_benefit_consumptions(payroll, from_failed_invoices_payroll_id)
                        self._mark_payroll_creation_finalizing(payroll, 0, 0)
                    self.create_accept_payroll_task(payroll.id, obj_data)

                if payroll_id_for_cleanup:
                    username = getattr(self.user, "login_name", None) or getattr(
                        self.user, "username", None
                    )
                    from payroll.opensearch_indexing_progress import (
                        begin_payroll_opensearch_indexing_after_db,
                        dispatch_payroll_opensearch_indexing_task,
                    )

                    self._complete_payroll_creation(payroll_id_for_cleanup)
                    begin_payroll_opensearch_indexing_after_db(
                        payroll_id_for_cleanup,
                        client_mutation_id=client_mutation_id,
                        username=username,
                    )
                    dispatch_payroll_opensearch_indexing_task(
                        payroll_id_for_cleanup,
                        client_mutation_id=client_mutation_id,
                        username=username,
                    )

            return dict_representation
        except Exception as exc:
            from payroll.creation_progress import (
                fail_payroll_creation_progress,
                fail_payroll_creation_progress_by_mutation,
            )

            username = getattr(self.user, "login_name", None) or getattr(
                self.user, "username", None
            )
            if payroll_id_for_cleanup:
                fail_payroll_creation_progress(
                    payroll_id_for_cleanup,
                    exc,
                    client_mutation_id=client_mutation_id,
                )
                from payroll.opensearch_indexing_progress import (
                    fail_payroll_opensearch_indexing,
                )

                fail_payroll_opensearch_indexing(
                    payroll_id_for_cleanup,
                    exc,
                    client_mutation_id=client_mutation_id,
                    username=username,
                )
            elif client_mutation_id:
                fail_payroll_creation_progress_by_mutation(client_mutation_id, exc)
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="create", exception=exc)

    @register_service_signal('payroll_service.update')
    def update(self, obj_data):
        raise NotImplementedError()

    @check_authentication
    @register_service_signal('payroll_service.delete')
    def delete(self, obj_data):
        payroll_to_delete = Payroll.objects.get(id=obj_data['id'])
        data = {'id': payroll_to_delete.id}
        TaskService(self.user).create({
            'source': 'payroll_delete',
            'entity': payroll_to_delete,
            'status': Task.Status.RECEIVED,
            'executor_action_event': TasksManagementConfig.default_executor_event,
            'business_event': PayrollConfig.payroll_delete_event,
            'data': _get_std_task_data_payload(data)
        })

    @check_authentication
    @register_service_signal('payroll_service.attach_benefit_to_payroll')
    def attach_benefit_to_payroll(self, payroll_id, benefit_id):
        payroll_benefit = PayrollBenefitConsumption(payroll_id=payroll_id, benefit_id=benefit_id)
        payroll_benefit.save(username=self.user.username)

    def attach_benefits_to_payroll_bulk(self, payroll_id, benefit_ids):
        """Lie plusieurs benefits à une paie en une requête bulk_create."""
        if not benefit_ids:
            return
        from datetime import datetime as py_datetime

        now = py_datetime.now()
        user = self.user
        links = []
        for benefit_id in benefit_ids:
            link = PayrollBenefitConsumption(payroll_id=payroll_id, benefit_id=benefit_id)
            link.set_pk()
            link.user_created = user
            link.user_updated = user
            link.date_created = now
            link.date_updated = now
            links.append(link)
        PayrollBenefitConsumption.objects.bulk_create(links, batch_size=500)

    @register_service_signal('payroll_service.create_task')
    def create_accept_payroll_task(self, payroll_id, obj_data):
        payroll_to_accept = Payroll.objects.get(id=payroll_id)
        payload = build_payroll_accept_task_payload(payroll_id, obj_data)
        TaskService(self.user).create({
            'source': 'payroll',
            'entity': payroll_to_accept,
            'status': Task.Status.RECEIVED,
            'executor_action_event': TasksManagementConfig.default_executor_event,
            'business_event': PayrollConfig.payroll_accept_event,
            'business_data_serializer': (
                f'{PayrollService.__module__}.{PayrollService.__name__}'
                '._accept_payroll_business_data_serializer'
            ),
            'data': payload,
        })

    @register_service_signal('payroll_service.close_payroll')
    def close_payroll(self, obj_data):
        from payroll.reconciliation_lock import (
            ensure_payroll_reconciliation_not_locked,
            is_payroll_reconciliation_locked,
        )

        payroll_to_close = Payroll.objects.get(id=obj_data['id'])
        # Enforce presence of at least one payment report (PDF) before closing
        has_report = PaymentReport.objects.filter(payroll=payroll_to_close, is_deleted=False).exists()
        if not has_report:
            return output_exception(model_name=self.OBJECT_TYPE.__name__, method="close_payroll",
                                    exception=ValueError(_('payment_report.required_before_closing')))
        if is_payroll_reconciliation_locked(payroll_to_close):
            return output_exception(
                model_name=self.OBJECT_TYPE.__name__,
                method="close_payroll",
                exception=ValueError(
                    _('At least one reconciliation close task already exists for this payroll.')
                ),
            )
        has_reconciled_benefit = BenefitConsumption.objects.filter(
            payrollbenefitconsumption__payroll=payroll_to_close,
            payrollbenefitconsumption__is_deleted=False,
            status=BenefitConsumptionStatus.RECONCILED,
            is_deleted=False,
        ).exists()
        if not has_reconciled_benefit:
            return output_exception(
                model_name=self.OBJECT_TYPE.__name__,
                method="close_payroll",
                exception=ValueError(
                    _('At least one benefit must be reconciled before Accept and Close.')
                ),
            )
        ensure_payroll_reconciliation_not_locked(payroll_to_close)
        TaskService(self.user).create({
            'source': 'payroll_reconciliation',
            'entity': payroll_to_close,
            'status': Task.Status.RECEIVED,
            'executor_action_event': TasksManagementConfig.default_executor_event,
            'business_event': PayrollConfig.payroll_reconciliation_event,
            'business_data_serializer': (
                f'{PayrollService.__module__}.{PayrollService.__name__}'
                '._reconciliation_business_data_serializer'
            ),
            'data': {
                'incoming_data': build_payroll_reconciliation_task_incoming_data(payroll_to_close),
            },
        })

    @register_service_signal('payroll_service.reject_approve_payroll')
    def reject_approved_payroll(self, obj_data):
        payroll_to_reject = Payroll.objects.get(id=obj_data['id'])
        data = {'id': payroll_to_reject.id}
        TaskService(self.user).create({
            'source': 'payroll_reject',
            'entity': payroll_to_reject,
            'status': Task.Status.RECEIVED,
            'executor_action_event': TasksManagementConfig.default_executor_event,
            'business_event': PayrollConfig.payroll_reject_event,
            'data': _get_std_task_data_payload(data)
        })

    def make_payment_for_payroll(self, obj_data):
        payroll = Payroll.objects.get(id=obj_data['id'])
        if payroll.payment_method != StrategyOnlinePayment.__name__:
            raise ValueError("Payroll payment method must be StrategyOnlinePayment")
        if is_payment_in_progress(payroll):
            raise ValueError("Payment is already in progress for this payroll")
        logger.info("[Payroll] make_payment_for_payroll: queuing task for payroll_id=%s", payroll.id)
        set_payment_in_progress(payroll, self.user, True)
        payroll_id = str(payroll.id)
        from payroll.payment_progress import (
            clear_payroll_payment_progress_cache,
            start_payroll_payment_progress,
        )

        clear_payroll_payment_progress_cache(payroll_id)
        username = getattr(self.user, "login_name", None) or self.user.username
        # Initialiser immédiatement la progression pour que le front affiche l'état "en cours"
        # même avant la première mise à jour du worker Celery.
        start_payroll_payment_progress(payroll_id, 0, username=username)
        user_id = self.user.id
        transaction.on_commit(
            lambda: send_requests_to_gateway_payment.delay(payroll_id, user_id)
        )

    @check_authentication
    def cancel_payment_for_payroll(self, obj_data):
        """
        Annule un job paiement (Celery révoqué, arrêt manuel admin, etc.).
        Remet payment_in_progress et payment_progress en état terminal pour le front.
        """
        from payroll.payment_progress import (
            cancel_payroll_payment_progress,
            persist_payment_progress_to_payroll,
        )

        payroll = Payroll.objects.get(id=obj_data['id'])
        reason = obj_data.get("reason") or "Payment cancelled."
        username = getattr(self.user, "login_name", None) or self.user.username
        set_payment_in_progress(payroll, self.user, False)
        progress = cancel_payroll_payment_progress(
            str(payroll.id), reason=reason, username=username
        )
        persist_payment_progress_to_payroll(payroll, progress, username)
        from payroll.mutation_log_task_bar import close_stale_payroll_mutations_for_payroll

        close_stale_payroll_mutations_for_payroll(payroll.id, reason=reason)
        return {"payroll_id": str(payroll.id), "payment_progress": progress}

    @check_authentication
    def cancel_reconciliation_for_payroll(self, obj_data):
        """
        Annule un job réconciliation (Celery révoqué, arrêt manuel admin, etc.).
        Remet reconciliation_in_progress et reconciliation_progress en état terminal.
        """
        from payroll.reconciliation_progress import (
            cancel_payroll_reconciliation_progress,
            persist_reconciliation_progress_to_payroll,
        )

        payroll = Payroll.objects.get(id=obj_data['id'])
        reason = obj_data.get("reason") or "Reconciliation cancelled."
        username = getattr(self.user, "login_name", None) or self.user.username
        set_reconciliation_in_progress(payroll, self.user, False)
        progress = cancel_payroll_reconciliation_progress(
            str(payroll.id), reason=reason, username=username
        )
        persist_reconciliation_progress_to_payroll(payroll, progress, username)
        from payroll.mutation_log_task_bar import close_stale_payroll_mutations_for_payroll

        close_stale_payroll_mutations_for_payroll(payroll.id, reason=reason)
        return {"payroll_id": str(payroll.id), "reconciliation_progress": progress}

    @check_authentication
    @register_service_signal('payroll_service.trigger_payroll_reconciliation')
    def trigger_payroll_reconciliation(self, obj_data):
        payroll = Payroll.objects.get(id=obj_data['id'])
        if payroll.payment_method != StrategyOnlinePayment.__name__:
            raise ValueError("Payroll payment method must be StrategyOnlinePayment")
        ensure_payroll_reconciliation_not_locked(payroll)
        if is_reconciliation_in_progress(payroll):
            raise ValueError("Reconciliation is already in progress for this payroll")
        logger.info("[Payroll] trigger_payroll_reconciliation: queuing task for payroll_id=%s", payroll.id)
        set_reconciliation_in_progress(payroll, self.user, True)
        payroll_id = str(payroll.id)
        user_id = self.user.id
        transaction.on_commit(
            lambda: send_request_to_reconcile.delay(payroll_id, user_id)
        )

    def _save_payroll(self, obj_data):
        obj_ = self.OBJECT_TYPE(**obj_data)
        dict_representation = self.save_instance(obj_)
        payroll_id = dict_representation["data"]["id"]
        payroll = Payroll.objects.get(id=payroll_id)
        return payroll, dict_representation

    def _get_payment_plan(self, obj_data):
        payment_plan_id = obj_data.get("payment_plan_id")
        payment_plan = PaymentPlan.objects.get(id=payment_plan_id)
        return payment_plan

    def _get_payment_cycle(self, obj_data):
        payment_cycle_id = obj_data.get("payment_cycle_id")
        payment_cycle = PaymentCycle.objects.get(id=payment_cycle_id)
        return payment_cycle

    def _get_dates_parameter(self, obj_data):
        date_valid_from = obj_data.get('date_valid_from', None)
        date_valid_to = obj_data.get('date_valid_to', None)
        return date_valid_from, date_valid_to

    def _resolve_json_ext_dict(self, raw):
        if not raw:
            return {}
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            import json

            try:
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                return {}
        return {}

    def _select_beneficiary_based_on_criteria(self, obj_data, payment_plan):
        """
        Filtre les bénéficiaires actifs du régime lié au plan de paiement.

        Ordre de priorité :
        1) json_ext envoyé dans createPayroll (critères spécifiques à cette paie)
        2) json_ext du plan de paiement (critères définis à la création du plan)
        """
        json_ext = self._resolve_json_ext_dict(obj_data.get("json_ext"))
        custom_filters = extract_custom_filters_from_json_ext(json_ext)

        if not custom_filters and payment_plan:
            plan_ext = self._resolve_json_ext_dict(payment_plan.json_ext)
            custom_filters = extract_custom_filters_from_json_ext(plan_ext)

        beneficiaries_queryset = Beneficiary.objects.filter(
            benefit_plan__id=payment_plan.benefit_plan.id,
            status=BeneficiaryStatus.ACTIVE,
            is_deleted=False,
        ).select_related("individual")

        if custom_filters:
            beneficiaries_queryset = CustomFilterWizardStorage.build_custom_filters_queryset(
                PayrollConfig.name,
                "BenefitPlan",
                custom_filters,
                beneficiaries_queryset,
                relation="individual",
            )

        return beneficiaries_queryset

    def _mark_payroll_creation_finalizing(self, payroll, processed, total):
        from payroll.creation_progress import (
            begin_payroll_creation_finalizing,
            persist_creation_progress_to_payroll,
        )

        progress = begin_payroll_creation_finalizing(str(payroll.id), processed, total)
        persist_creation_progress_to_payroll(payroll, progress, self.user.username)

    def _complete_payroll_creation(self, payroll_id):
        from payroll.creation_progress import (
            complete_payroll_creation_progress,
            persist_creation_progress_to_payroll,
        )

        payroll = Payroll.objects.filter(id=payroll_id, is_deleted=False).first()
        if not payroll:
            return
        progress = complete_payroll_creation_progress(str(payroll_id))
        persist_creation_progress_to_payroll(payroll, progress, self.user.username)

    def _generate_benefits(
        self,
        payment_plan,
        beneficiaries_queryset,
        date_from,
        date_to,
        payroll,
        payment_cycle,
        client_mutation_id=None,
    ):
        from payroll.creation_progress import fail_payroll_creation_progress

        calculation = get_calculation_object(payment_plan.calculation)
        total = beneficiaries_queryset.count()
        try:
            calculation.calculate_if_active_for_object(
                payment_plan,
                user_id=self.user.id,
                start_date=date_from,
                end_date=date_to,
                beneficiaries_queryset=beneficiaries_queryset,
                payroll=payroll,
                payment_cycle=payment_cycle,
                client_mutation_id=client_mutation_id,
            )
            self._mark_payroll_creation_finalizing(payroll, total, total)
        except Exception as exc:
            fail_payroll_creation_progress(str(payroll.id), exc, client_mutation_id=client_mutation_id)
            raise

    @transaction.atomic
    def _move_benefit_consumptions(self, payroll, from_payroll_id):
        payroll_benefits = PayrollBenefitConsumption.objects.filter(
            payroll_id=from_payroll_id,
            benefit__status__in=[BenefitConsumptionStatus.ACCEPTED, BenefitConsumptionStatus.APPROVE_FOR_PAYMENT]
        )
        payroll_benefits.update(payroll=payroll)
        benefits = BenefitConsumption.objects.filter(payrollbenefitconsumption__payroll=payroll)
        benefits.update(status=BenefitConsumptionStatus.ACCEPTED)

    @staticmethod
    def _accept_payroll_business_data_serializer(data):
        """Liste des bénéficiaires / factures pour la tâche d'acceptation de paie."""
        return build_payroll_accept_task_display(data)

    @staticmethod
    def _reconciliation_business_data_serializer(data):
        """
        Formate les données de la tâche payroll_reconciliation pour l'écran de validation.
        """
        if not data:
            return data

        incoming = data.get("incoming_data", data)
        recap = (
            resolve_reconciliation_recap_from_task_data(incoming)
            if incoming
            else {}
        )
        counts = recap.get("counts") or {}
        amounts = recap.get("amounts") or {}
        payroll_info = recap.get("payroll") or {}
        last_run = recap.get("last_run") or {}

        formatted = {
            "incoming_data": {
                "payroll": payroll_info.get("name") or incoming.get("payroll_name"),
                "payroll_id": str(incoming.get("id") or payroll_info.get("id") or ""),
                "statut_paie": incoming.get("statut_paie") or payroll_info.get("status"),
                "total_factures": incoming.get("total_factures", counts.get("total", 0)),
                "factures_reconciliees": incoming.get(
                    "factures_reconciliees", counts.get("reconciled", 0)
                ),
                "factures_en_attente": incoming.get(
                    "factures_en_attente", counts.get("approve_for_payment", 0)
                ),
                "autres_statuts": incoming.get("autres_statuts", counts.get("other", 0)),
                "montant_total": incoming.get("montant_total") or amounts.get("total"),
                "montant_reconcilie": incoming.get("montant_reconcilie") or amounts.get("reconciled"),
                "montant_en_attente": incoming.get("montant_en_attente") or amounts.get("pending"),
                "derniere_reconciliation": (
                    incoming.get("derniere_reconciliation") or recap.get("last_completed_at")
                ),
                "dernier_run_succes": last_run.get("success_count"),
                "dernier_run_rejets": last_run.get("rejected_count"),
                "rapports_paiement": incoming.get("rapports_paiement") or ", ".join(
                    report.get("file_name")
                    for report in (recap.get("payment_reports") or [])
                    if report.get("file_name")
                ),
                "recapitulatif_reconciliation": (
                    incoming.get("recapitulatif_reconciliation")
                    or (format_reconciliation_recap_text(recap) if recap else None)
                ),
                "detail_factures_reconciliees": [
                    {
                        "facture": row.get("code"),
                        "montant": row.get("amount"),
                        "recu": row.get("receipt"),
                        "source": row.get("reconciliation_source"),
                    }
                    for row in (recap.get("reconciled_benefits") or [])
                ],
                "detail_factures_en_attente": [
                    {
                        "facture": row.get("code"),
                        "montant": row.get("amount"),
                        "statut": row.get("status"),
                    }
                    for row in (recap.get("pending_benefits") or [])
                ],
                "nombre_benefices_trouves": recap.get(
                    "nombre_benefices_trouves", counts.get("total", 0)
                ),
                "nombre_beneficiaires_selectionnes": recap.get(
                    "nombre_beneficiaires_selectionnes", counts.get("total", 0)
                ),
                "nombre_factures_reconciliees": recap.get(
                    "nombre_factures_reconciliees", counts.get("reconciled", 0)
                ),
                "texte_reconciliees_sur_total": recap.get("texte_reconciliees_sur_total"),
            },
            "reconciliation_recap": recap,
        }
        if recap.get("benefits_truncated"):
            formatted["incoming_data"]["note_liste_tronquee"] = (
                "La liste détaillée des factures réconciliées est tronquée pour l'affichage."
            )
        return formatted

    # Ancien chemin enregistré sur les tâches existantes (réconciliation).
    _business_data_serializer = _reconciliation_business_data_serializer


class BenefitConsumptionService(BaseService):
    OBJECT_TYPE = BenefitConsumption

    def __init__(self, user, validation_class=BenefitConsumptionValidation):
        super().__init__(user, validation_class)

    @check_authentication
    @register_service_signal('benefit_consumption_service.create')
    def create(self, obj_data):
        return super().create(obj_data)

    @register_service_signal('benefit_consumption_service.update')
    def update(self, obj_data):
        return super().update(obj_data)

    @check_authentication
    @register_service_signal('benefit_consumption_service.delete')
    def delete(self, obj_data):
        benefit_to_delete = BenefitConsumption.objects.get(id=obj_data['id'])
        benefit_to_delete.status = BenefitConsumptionStatus.PENDING_DELETION
        benefit_to_delete.save(username=self.user.username)
        data = {'id': benefit_to_delete.id}
        TaskService(self.user).create({
            'source': 'benefit_delete',
            'entity': benefit_to_delete,
            'status': Task.Status.RECEIVED,
            'executor_action_event': TasksManagementConfig.default_executor_event,
            'business_event': PayrollConfig.benefit_delete_event,
            'data': _get_std_task_data_payload(data)
        })

    @check_authentication
    @register_service_signal('benefit_consumption_service.create_or_update_benefit_attachment')
    def create_or_update_benefit_attachment(self, bills_queryset, benefit_id):
        # remove first old attachments and save the new one
        BenefitAttachment.objects.filter(benefit_id=benefit_id).delete()
        # save new bill attachments
        for bill in bills_queryset:
            benefit_attachment = BenefitAttachment(bill_id=bill.id, benefit_id=benefit_id)
            benefit_attachment.save(username=self.user.username)

    def create_benefit_attachment_for_bill(self, bill_id, benefit_id):
        """Crée une pièce jointe facture/benefit sans DELETE (benefit neuf à la création paie)."""
        benefit_attachment = BenefitAttachment(bill_id=bill_id, benefit_id=benefit_id)
        benefit_attachment.save(username=self.user.username)


class CsvReconciliationService:
    def __init__(self, user: InteractiveUser):
        self.user = user

    @staticmethod
    def _strip_accents(value):
        if value is None:
            return value
        text = str(value)
        normalized = unicodedata.normalize("NFKD", text)
        return "".join(c for c in normalized if not unicodedata.combining(c))

    def _normalize_upload_dataframe_columns(self, df):
        """
        En-têtes CSV (souvent sans accents après export) -> clés internes attendues par la validation.
        """
        df.columns = [self._strip_accents(col) for col in df.columns]
        reverse_map = {
            self._strip_accents(v): k
            for k, v in PayrollConfig.csv_reconciliation_field_mapping.items()
        }
        df.rename(columns=reverse_map, inplace=True)
        paid_internal = PayrollConfig.csv_reconciliation_paid_extra_field
        paid_header = self._strip_accents(paid_internal)
        if paid_header in df.columns and paid_header != paid_internal:
            df.rename(columns={paid_header: paid_internal}, inplace=True)

    def _display_column_names_for_export(self):
        """Clés internes -> libellés CSV (sans accents), pour fichiers retournés à l'utilisateur."""
        display = {
            k: self._strip_accents(v)
            for k, v in PayrollConfig.csv_reconciliation_field_mapping.items()
        }
        paid_internal = PayrollConfig.csv_reconciliation_paid_extra_field
        display[paid_internal] = self._strip_accents(paid_internal)
        return display

    def download_reconciliation(self, payroll_id) -> BytesIO:
        payroll = self._resolve_payroll(payroll_id)
        bc_qs = self._get_benefit_consumption_qs(payroll)
        # Retrieve the basic fields
        field_keys = list(PayrollConfig.csv_reconciliation_field_mapping.keys())
        records = list(bc_qs.values(*field_keys))

        # Collect all extra_info keys to ensure all columns are present in the DataFrame
        extra_info_keys = set()
        extra_info_dicts = []  # To store extra_info dicts for each record
        # Build a lookup to avoid per-record queries
        benefits_by_code = {b.code: b for b in bc_qs.select_related('individual')}
        # Pre-fill custom columns from best-effort sources
        prefill_code_menage = []
        prefill_numero_paie = []
        prefill_code_client = []
        observed_schema_keys = set(extra_info_keys)
        for record in records:
            bc = benefits_by_code.get(record['code'])
            extra_info = bc.json_ext.get('extra_info', {}) if bc.json_ext else {}
            extra_info_keys.update(extra_info.keys())
            extra_info_dicts.append(extra_info)
            # code_menage: prefer extra_info, else individual.json_ext
            ind_json = bc.individual.json_ext if getattr(bc, 'individual', None) else None
            if isinstance(ind_json, dict):
                observed_schema_keys.update(ind_json.keys())
            if isinstance(extra_info, dict):
                observed_schema_keys.update(extra_info.keys())
            code_menage = extra_info.get('code_menage') if extra_info else None
            if code_menage is None and ind_json:
                code_menage = (ind_json or {}).get('code_menage')
            prefill_code_menage.append(code_menage)
            # numero_paie: prefer extra_info, fallback to individual.json_ext.
            numero_paie = extra_info.get('numero_paie') if extra_info else None
            if numero_paie is None and ind_json:
                numero_paie = (ind_json or {}).get('numero_paie')
            prefill_numero_paie.append(numero_paie)
            # code_client: prefer extra_info, fallback to legacy key then individual.json_ext.
            code_client = extra_info.get('code_client') if extra_info else None
            if code_client is None:
                code_client = extra_info.get('code_empreinte') if extra_info else None
            if code_client is None and ind_json:
                code_client = (ind_json or {}).get('code_client')
            if code_client is None and ind_json:
                code_client = (ind_json or {}).get('code_empreinte')
            prefill_code_client.append(code_client)

        # Convert to DataFrame
        df = pd.DataFrame.from_records(records)

        for key in extra_info_keys:
            if key not in df.columns:
                df[key] = None

        # Add paid extra field
        df[PayrollConfig.csv_reconciliation_paid_extra_field] = df.apply(
            lambda row: self._fill_paid_column(row), axis=1
        )
        df.rename(columns=PayrollConfig.csv_reconciliation_field_mapping, inplace=True)
        # Suppression des accents dans les en-têtes exportés (compatibles upload)
        df.columns = [self._strip_accents(col) for col in df.columns]

        # Add extra_info fields at the end of the DataFrame
        for key in extra_info_keys:
            df[key] = [extra_info_dict.get(key, None) for extra_info_dict in extra_info_dicts]

        # Ensure additional custom columns exist (fallback if ModuleConfiguration override)
        additional_columns = self._resolve_optional_additional_columns(
            self._get_additional_columns(),
            observed_schema_keys,
        )
        for col in additional_columns:
            if col not in df.columns:
                df[col] = None
        # Pre-fill additional custom columns when possible
        if 'code_menage' in df.columns and prefill_code_menage:
            df['code_menage'] = prefill_code_menage
        if 'numero_paie' in df.columns and prefill_numero_paie:
            df['numero_paie'] = prefill_numero_paie
        if 'code_client' in df.columns and prefill_code_client:
            df['code_client'] = prefill_code_client

        in_memory_file = BytesIO()
        # BytesIO is duck-typed as a file object, so it can be passed to df.to_csv
        # noinspection PyTypeChecker
        df.to_csv(in_memory_file, index=False)
        return in_memory_file

    def upload_reconciliation(self, payroll_id, file, upload):
        payroll = self._resolve_payroll(payroll_id)
        upload.payroll = payroll
        upload.status = upload.Status.IN_PROGRESS
        upload.save(username=self.user.login_name)
        if not file:
            raise ValueError(_('csv_reconciliation.validation.file_required'))
        
        # Lecture uniquement CSV
        df = pd.read_csv(file)
        # Compatibilité ancienne colonne: code_empreinte -> code_client
        if 'code_empreinte' in df.columns and 'code_client' not in df.columns:
            df.rename(columns={'code_empreinte': 'code_client'}, inplace=True)

        self._normalize_upload_dataframe_columns(df)
        self._validate_dataframe(df)

        code_col = PayrollConfig.csv_reconciliation_code_column
        codes = [c for c in df.get(code_col, pd.Series(dtype="object")).dropna().unique().tolist() if str(c).strip()]
        benefits_by_code = {}
        payroll_codes = set()
        if codes:
            benefit_qs = BenefitConsumption.objects.filter(
                code__in=codes,
                is_deleted=False,
            ).select_related('individual')
            benefits_by_code = {bc.code: bc for bc in benefit_qs}
            payroll_codes = set(
                BenefitConsumption.objects.filter(
                    code__in=codes,
                    is_deleted=False,
                    payrollbenefitconsumption__payroll=payroll,
                    payrollbenefitconsumption__is_deleted=False,
                ).values_list("code", flat=True)
            )

        bills_by_benefit_id = self._prefetch_bills_by_benefit_id(
            [b.id for b in benefits_by_code.values()]
        )
        from payroll.opensearch_payroll_status_sync import (
            skip_heavy_opensearch_reindex,
            sync_benefit_status_batch_to_opensearch,
        )

        affected_rows = 0
        skipped_items = 0
        total_number_of_benefits_in_file = len(df)
        reconciled_benefit_ids = []
        error_values = []
        t0 = time.perf_counter()

        payment_service = PaymentInvoiceService(self.user)
        bill_content_type = ContentType.objects.get_for_model(Bill)

        with skip_heavy_opensearch_reindex():
            for idx in range(len(df)):
                row = df.iloc[idx]
                errors, bc = self._validate_reconciliation_row(
                    payroll, row, benefits_by_code, payroll_codes
                )
                if (
                    not errors
                    and bc
                    and self._should_apply_csv_reconciliation(row, bc)
                ):
                    self._apply_csv_reconciliation_row(
                        row,
                        bc,
                        bill=bills_by_benefit_id.get(bc.id),
                        payment_service=payment_service,
                        bill_content_type=bill_content_type,
                    )
                    reconciled_benefit_ids.append(bc.id)
                error_values.append(errors)
                if errors:
                    skipped_items += 1
                else:
                    affected_rows += 1

            if reconciled_benefit_ids:
                os_batch = 500
                for offset in range(0, len(reconciled_benefit_ids), os_batch):
                    sync_benefit_status_batch_to_opensearch(
                        payroll.id,
                        reconciled_benefit_ids[offset : offset + os_batch],
                        BenefitConsumptionStatus.RECONCILED,
                    )

        elapsed = time.perf_counter() - t0
        logger.info(
            "CSV reconciliation upload payroll_id=%s rows=%s reconciled=%s skipped=%s elapsed=%.2fs",
            payroll_id,
            total_number_of_benefits_in_file,
            len(reconciled_benefit_ids),
            skipped_items,
            elapsed,
        )

        df[PayrollConfig.csv_reconciliation_errors_column] = error_values

        summary = {
            'affected_rows': affected_rows,
            'total_number_of_benefits_in_file': total_number_of_benefits_in_file,
            'skipped_items': skipped_items
        }

        error_df = df[df[PayrollConfig.csv_reconciliation_errors_column].apply(lambda x: bool(x))]
        if not error_df.empty:
            in_memory_file = BytesIO()
            df.rename(columns=self._display_column_names_for_export(), inplace=True)
            df.to_csv(in_memory_file, index=False)
            return in_memory_file, error_df.set_index(PayrollConfig.csv_reconciliation_code_column)\
                                   [PayrollConfig.csv_reconciliation_errors_column].to_dict(), summary
        return file, None, summary

    def _get_benefit_consumption_qs(self, payroll):
        qs = BenefitConsumption.objects.filter(payrollbenefitconsumption__payroll=payroll, is_deleted=False)
        if not qs.exists():
            raise ValueError('csv_reconciliation.validation.no_benefit_consumption_for_payroll')
        return qs

    def _validate_dataframe(self, df):
        if df is None:
            raise ValueError(_("Unknown error while loading import file"))
        if df.empty:
            raise ValueError(_("Import file is empty"))
        if PayrollConfig.csv_reconciliation_errors_column in df.columns:
            raise ValueError(_("Column errors in csv."))
        status_col = "status"
        if status_col in df.columns:
            if (df[status_col] == BenefitConsumptionStatus.RECONCILED).all():
                raise ValueError(_("All of the Benefit Consumptions have been already reconciled."))

    def _fill_paid_column(self, row):
        if (PayrollConfig.csv_reconciliation_status_column in row
                and row[PayrollConfig.csv_reconciliation_status_column] == BenefitConsumptionStatus.RECONCILED):
            return PayrollConfig.csv_reconciliation_paid_yes
        else:
            return None

    def _resolve_payroll(self, payroll_id):
        if not payroll_id:
            raise ValueError('csv_reconciliation.validation.payroll_id_required')
        payroll = Payroll.objects.filter(id=payroll_id, is_deleted=False).first()
        if not payroll:
            raise ValueError('csv_reconciliation.validation.payroll_not_found')
        return payroll

    def _get_additional_columns(self):
        _default = ["code_menage", "numero_paie", "code_client"]
        try:
            cols = getattr(PayrollConfig, 'csv_reconciliation_additional_columns', None) or _default
        except Exception:
            cols = _default
        normalized = []
        for col in cols:
            if col == "code_empreinte":
                col = "code_client"
            if col not in normalized:
                normalized.append(col)
        return normalized

    def _resolve_optional_additional_columns(self, columns, observed_schema_keys):
        """
        Ajoute code_client / numero_paie uniquement s'ils existent dans le schéma de données
        (keys observées dans individual.json_ext ou benefit.json_ext.extra_info).
        """
        cols = list(columns or [])
        schema_keys = set(observed_schema_keys or set())
        if "numero_paie" in cols and "numero_paie" not in schema_keys:
            cols.remove("numero_paie")
        code_client_keys = {"code_client", "code_empreinte"}
        if "code_client" in cols and not (schema_keys & code_client_keys):
            cols.remove("code_client")
        return cols

    def _prefetch_bills_by_benefit_id(self, benefit_ids):
        if not benefit_ids:
            return {}
        bills_by_benefit_id = {}
        attachments = BenefitAttachment.objects.filter(
            benefit_id__in=benefit_ids,
            is_deleted=False,
        ).select_related("bill")
        for attachment in attachments:
            if attachment.benefit_id not in bills_by_benefit_id and attachment.bill_id:
                bills_by_benefit_id[attachment.benefit_id] = attachment.bill
        return bills_by_benefit_id

    @staticmethod
    def _normalize_paid_value(row):
        paid_val = row[PayrollConfig.csv_reconciliation_paid_extra_field]
        if pd.isna(paid_val):
            return None
        text = str(paid_val).strip()
        return text or None

    @staticmethod
    def _normalize_receipt_value(row):
        receipt_val = row[PayrollConfig.csv_reconciliation_receipt_column]
        if pd.isna(receipt_val):
            return None
        text = str(receipt_val).strip()
        return text or None

    def _should_apply_csv_reconciliation(self, row, bc):
        paid_val = self._normalize_paid_value(row)
        return (
            paid_val == PayrollConfig.csv_reconciliation_paid_yes
            and bc.status == BenefitConsumptionStatus.ACCEPTED
        )

    def _validate_additional_columns(self, row, bc, errors):
        """Valide le format et la cohérence des colonnes additionnelles."""
        max_length = 255
        for col in self._get_additional_columns():
            if col not in row.index:
                continue
            val = row[col]
            val_str = "" if (pd.isna(val) or not str(val).strip()) else str(val).strip()

            # code_menage : requis et identique à individual.json_ext["code_menage"]
            if col == 'code_menage' and bc and getattr(bc, 'individual', None):
                ind_json = (bc.individual.json_ext or {}) if bc.individual.json_ext else {}
                ind_code_menage = ind_json.get('code_menage')
                ind_code_menage_str = (
                    str(ind_code_menage).strip() if ind_code_menage is not None and str(ind_code_menage).strip()
                    else None
                )
                if ind_code_menage_str is None:
                    errors.append(_('Individual must have code_menage (household code)'))
                elif not val_str:
                    errors.append(_('code_menage is required in the reconciliation file'))
                elif val_str != ind_code_menage_str:
                    errors.append(
                        _('code_menage must match the individual household code: expected "%(expected)s"')
                        % {'expected': ind_code_menage_str}
                    )
                continue

            if not val_str:
                continue
            if len(val_str) > max_length:
                errors.append(
                    _('Column "%(column)s" exceeds maximum length of %(max)s characters')
                    % {'column': col, 'max': max_length}
                )

    def _validate_reconciliation_row(self, payroll, row, benefits_by_code=None, payroll_codes=None):
        errors = []
        code_value = row[PayrollConfig.csv_reconciliation_code_column]
        code_value = str(code_value).strip() if not pd.isna(code_value) else None
        bc = (benefits_by_code or {}).get(code_value)
        if bc is None and code_value:
            bc = BenefitConsumption.objects.filter(
                code=code_value, is_deleted=False
            ).select_related('individual').first()
        if not bc:
            errors.append(_('benefit_consumption_not_found'))
        if bc and payroll_codes is not None and code_value not in payroll_codes:
            errors.append(_('benefit_consumption_not_in_payroll'))
        elif bc and payroll_codes is None and not bc.payrollbenefitconsumption_set.filter(payroll=payroll).exists():
            errors.append(_('benefit_consumption_not_in_payroll'))
        paid_val = self._normalize_paid_value(row)
        if paid_val and paid_val not in [PayrollConfig.csv_reconciliation_paid_yes, PayrollConfig.csv_reconciliation_paid_no]:
            errors.append(_('paid_column_invalid_value'))

        if paid_val == PayrollConfig.csv_reconciliation_paid_yes and not self._normalize_receipt_value(row):
            errors.append(_('receipt_required'))

        if bc and bc.status != row['status']:
            errors.append(_('status_not_matching'))

        self._validate_additional_columns(row, bc, errors)

        return (errors if errors else None), bc

    def _reconcile_row(self, payroll, row, benefits_by_code=None, payroll_codes=None):
        """Compatibilité tests / appels directs : validation seule."""
        errors, _bc = self._validate_reconciliation_row(
            payroll, row, benefits_by_code, payroll_codes
        )
        return errors

    def _apply_csv_reconciliation_row(
        self,
        row,
        bc,
        *,
        bill=None,
        payment_service=None,
        bill_content_type=None,
    ):
        self._reconcile_bc(
            row,
            bc,
            bill=bill,
            payment_service=payment_service,
            bill_content_type=bill_content_type,
        )

    def _reconcile_bc(self, row, bc, *, bill=None, payment_service=None, bill_content_type=None):
        from payroll.opensearch_payroll_status_sync import benefit_status_only_save

        bc.status = BenefitConsumptionStatus.RECONCILED
        bc.receipt = self._normalize_receipt_value(row)
        excluded = set(PayrollConfig.csv_reconciliation_field_mapping)
        excluded.add(PayrollConfig.csv_reconciliation_errors_column)
        extra_info = {
            k: row[k] for k in row.index
            if k not in excluded and not pd.isna(row[k]) and str(row[k]).strip()
        }
        bc.json_ext = {'extra_info': extra_info}
        benefit_status_only_save(bc, self.user.login_name)
        if bill is None:
            bill = Bill.objects.filter(benefitattachment__benefit=bc, is_deleted=False).first()
        if bill:
            self._reconcile_bill(
                row,
                bill,
                payment_service=payment_service,
                bill_content_type=bill_content_type,
            )

    def _reconcile_bill(self, row, bill, *, payment_service=None, bill_content_type=None):
        current_date = datetime.date.today()
        bill.status = Bill.Status.RECONCILIATED
        bill.date_payed = current_date
        from payroll.opensearch_payroll_status_sync import skip_heavy_opensearch_reindex

        with skip_heavy_opensearch_reindex():
            bill.save(username=self.user.login_name)

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
            'subject_type': bill_content_type or ContentType.objects.get_for_model(bill),
            'subject': bill,
            'status': DetailPaymentInvoice.DetailPaymentStatus.ACCEPTED,
            'fees': 0.0,
            'amount': bill.amount_total,
            'reconcilation_id': self._normalize_receipt_value(row),
            'reconcilation_date': current_date,
        }
        bill_payment_details = DetailPaymentInvoice(**bill_payment_details)
        service = payment_service or PaymentInvoiceService(self.user)
        service.create_with_detail(bill_payment, bill_payment_details)
