import graphene
from django.db.models import Sum, Q
from graphene_django import DjangoObjectType

from core import prefix_filterset, ExtendedConnection
from core.gql_queries import UserGQLType
from core.utils import DefaultStorageFileHandler
from invoice.gql.gql_types.bill_types import BillGQLType
from location.gql_queries import LocationGQLType
from individual.gql_queries import IndividualGQLType
from payroll.models import PaymentPoint, Payroll, BenefitConsumption, \
    PayrollBenefitConsumption, BenefitAttachment, CsvReconciliationUpload
from payroll.payroll_reconciliation_status import (
    MAX_BENEFITS_IN_TASK_RECAP,
    _payroll_benefits_qs,
    can_close_payroll,
    count_payroll_benefits,
    count_reconciled_benefits,
    deep_to_json_safe,
    get_close_payroll_blockers,
    has_payment_report,
    resolve_last_reconciliation_at,
    resolve_payroll_reconciliation_recap,
)
from payroll.reconciliation_lock import (
    is_payment_in_progress,
    is_payroll_reconciliation_locked,
    is_reconciliation_in_progress,
)
from contribution_plan.gql import PaymentPlanGQLType
from payment_cycle.gql_queries import PaymentCycleGQLType
from social_protection.models import BenefitPlan


class PaymentPointGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = PaymentPoint
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "name": ["iexact", "istartswith", "icontains"],
            **prefix_filterset("location__", LocationGQLType._meta.filter_fields),
            **prefix_filterset("ppm__", UserGQLType._meta.filter_fields),

            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection


class BenefitAttachmentGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = BenefitAttachment
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            **prefix_filterset("bill__", BillGQLType._meta.filter_fields),

            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_from": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_to": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection


class BenefitConsumptionGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')
    benefit_attachment = graphene.List(BenefitAttachmentGQLType)

    class Meta:
        model = BenefitConsumption
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "photo": ["iexact", "istartswith", "icontains"],
            "code": ["iexact", "istartswith", "icontains"],
            "status": ["exact", "startswith", "icontains", "contains"],
            "receipt": ["exact", "startswith", "icontains"],
            "type": ["exact", "startswith", "icontains"],
            "amount": ["exact", "lt", "lte", "gt", "gte"],
            "date_due": ["exact", "lt", "lte", "gt", "gte"],
            **prefix_filterset("individual__", IndividualGQLType._meta.filter_fields),

            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_from": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_to": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection

    def resolve_benefit_attachment(self, info):
        cached = getattr(self, "_prefetched_objects_cache", None)
        if cached and "benefitattachment_set" in cached:
            return list(self.benefitattachment_set.all())
        return BenefitAttachment.objects.filter(
            benefit_id=self.id,
            is_deleted=False,
        )


class PayrollGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')
    benefit_consumption = graphene.List(BenefitConsumptionGQLType)
    benefit_plan_name_code = graphene.String()
    reconciliation_in_progress = graphene.Boolean()
    payment_in_progress = graphene.Boolean()
    reconciliation_locked = graphene.Boolean()
    has_payment_report = graphene.Boolean()
    reconciled_benefit_count = graphene.Int()
    can_close_payroll = graphene.Boolean()
    close_payroll_blockers = graphene.List(graphene.String)
    reconciliation_last_completed_at = graphene.String()
    reconciliation_last_summary = graphene.JSONString()
    reconciliation_recap = graphene.JSONString()
    benefit_consumption_truncated = graphene.Boolean()
    benefit_consumption_total_count = graphene.Int()
    benefices_trouves = graphene.Int(
        description="Total factures/bénéfices de la paie (équivalent benefitConsumptionTotalCount).",
    )
    beneficiaires_selectionnes = graphene.Int(
        description=(
            "Total bénéficiaires inclus dans la paie. "
            "Ne pas utiliser benefitConsumption.length (liste limitée à 100 pour l'aperçu)."
        ),
    )
    creation_progress = graphene.JSONString()
    payment_progress = graphene.JSONString()
    reconciliation_progress = graphene.JSONString()
    payment_approved_modal_summary = graphene.JSONString(
        description=(
            "Modale paiements approuvés : gatewayApprovedCount (passerelle), "
            "reconciledPercent, libellés texte."
        ),
    )
    payment_reconciled_modal_summary = graphene.JSONString(
        description=(
            "Modale paiements réconciliés : mêmes indicateurs que paymentApprovedModalSummary "
            "(gatewayApprovedCount, reconciledPercent, libellés texte)."
        ),
    )

    class Meta:
        model = Payroll
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            "name": ["iexact", "istartswith", "icontains"],
            "status": ["exact", "startswith", "contains"],
            "payment_method": ["exact", "startswith", "contains"],
            **prefix_filterset("payment_point__", PaymentPointGQLType._meta.filter_fields),
            **prefix_filterset("payment_plan__", PaymentPlanGQLType._meta.filter_fields),
            **prefix_filterset("payment_cycle__", PaymentCycleGQLType._meta.filter_fields),

            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_from": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_to": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection

    @classmethod
    def get_queryset(cls, queryset, info):
        return queryset.order_by("-date_created")

    def resolve_benefit_consumption(self, info):
        # Ne jamais renvoyer toute la paie ici : le front doit utiliser
        # benefitConsumptionByPayroll (paginé) ou reconciliationRecap pour le résumé.
        return (
            _payroll_benefits_qs(self)
            .order_by("code")
            .only("id", "code", "amount", "status", "receipt", "json_ext")[:MAX_BENEFITS_IN_TASK_RECAP]
        )

    def resolve_benefit_consumption_truncated(self, info):
        return count_payroll_benefits(self) > MAX_BENEFITS_IN_TASK_RECAP

    def resolve_benefit_consumption_total_count(self, info):
        return count_payroll_benefits(self)

    def resolve_benefices_trouves(self, info):
        return count_payroll_benefits(self)

    def resolve_beneficiaires_selectionnes(self, info):
        return count_payroll_benefits(self)

    def resolve_reconciliation_recap(self, info):
        return deep_to_json_safe(resolve_payroll_reconciliation_recap(self))

    def resolve_benefit_plan_name_code(self, info):
        benefit_plan = BenefitPlan.objects.get(id=self.payment_plan.benefit_plan.id, is_deleted=False)
        return f"{benefit_plan.code} - {benefit_plan.name}"

    def resolve_reconciliation_in_progress(self, info):
        return is_reconciliation_in_progress(self)

    def resolve_payment_in_progress(self, info):
        return is_payment_in_progress(self)

    def resolve_reconciliation_locked(self, info):
        return is_payroll_reconciliation_locked(self)

    def resolve_has_payment_report(self, info):
        return has_payment_report(self)

    def resolve_reconciled_benefit_count(self, info):
        return count_reconciled_benefits(self)

    def resolve_can_close_payroll(self, info):
        return can_close_payroll(self)

    def resolve_close_payroll_blockers(self, info):
        return get_close_payroll_blockers(self)

    def resolve_reconciliation_last_completed_at(self, info):
        return resolve_last_reconciliation_at(self)

    def resolve_reconciliation_last_summary(self, info):
        return (self.json_ext or {}).get("reconciliation_last_summary")

    def resolve_creation_progress(self, info):
        from payroll.creation_progress import get_payroll_creation_progress

        live = get_payroll_creation_progress(str(self.id))
        if live:
            return live
        return (self.json_ext or {}).get("creation_progress")

    def resolve_payment_progress(self, info):
        from payroll.payment_progress import reconcile_payment_progress_for_payroll

        return reconcile_payment_progress_for_payroll(self)

    def resolve_reconciliation_progress(self, info):
        from payroll.reconciliation_progress import reconcile_reconciliation_progress_for_payroll

        return reconcile_reconciliation_progress_for_payroll(self)

    def resolve_payment_approved_modal_summary(self, info):
        from payroll.payment_approved_modal_summary import build_payment_approved_modal_summary

        return build_payment_approved_modal_summary(self)

    def resolve_payment_reconciled_modal_summary(self, info):
        from payroll.payment_reconciled_modal_summary import build_payment_reconciled_modal_summary

        return build_payment_reconciled_modal_summary(self)


class PaymentMethodGQLType(graphene.ObjectType):
    name = graphene.String()


class PaymentGatewayConfigGQLType(graphene.ObjectType):
    base_url = graphene.String()
    api_key = graphene.String()
    timeout = graphene.Int()


class PaymentMethodListGQLType(graphene.ObjectType):
    payment_methods = graphene.List(PaymentMethodGQLType)


class BenefitAttachmentListGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = BenefitAttachment
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            **prefix_filterset("bill__", BillGQLType._meta.filter_fields),
            **prefix_filterset("benefit__", BenefitConsumptionGQLType._meta.filter_fields),

            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_from": ["exact", "lt", "lte", "gt", "gte"],
            "date_valid_to": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection


class CsvReconciliationUploadGQLType(DjangoObjectType):
    uuid = graphene.String(source='uuid')

    class Meta:
        model = CsvReconciliationUpload
        interfaces = (graphene.relay.Node,)

        filter_fields = {
            "id": ["exact"],
            "file_name": ["exact", "iexact", "istartswith", "icontains"],
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "status": ["exact", "iexact", "istartswith", "icontains"],
            "is_deleted": ["exact"],
            "version": ["exact"],
            **prefix_filterset("payroll__", PayrollGQLType._meta.filter_fields),
        }
        connection_class = ExtendedConnection


class PayrollBenefitConsumptionGQLType(DjangoObjectType):

    class Meta:
        model = PayrollBenefitConsumption
        interfaces = (graphene.relay.Node,)
        filter_fields = {
            "id": ["exact"],
            **prefix_filterset("payroll__", PayrollGQLType._meta.filter_fields),
            **prefix_filterset("benefit__", BenefitConsumptionGQLType._meta.filter_fields),
            "date_created": ["exact", "lt", "lte", "gt", "gte"],
            "date_updated": ["exact", "lt", "lte", "gt", "gte"],
            "is_deleted": ["exact"],
            "version": ["exact"],
        }
        connection_class = ExtendedConnection


class BenefitsSummaryGQLType(graphene.ObjectType):
    total_amount_received = graphene.String()
    total_amount_due = graphene.String()
