import graphene
import graphene_django_optimizer as gql_optimizer
from gettext import gettext as _
from django.contrib.auth.models import AnonymousUser
from django.db.models import Q, Sum

from core.schema import OrderedDjangoFilterConnectionField
from core.services import wait_for_mutation
from core.utils import append_validity_filter
from invoice.gql.gql_types.bill_types import BillGQLType
from invoice.models import Bill
from location.services import get_ancestor_location_filter
from payroll.apps import PayrollConfig
from payroll.gql_mutations import CreatePaymentPointMutation, UpdatePaymentPointMutation, DeletePaymentPointMutation, \
    CreatePayrollMutation, DeletePayrollMutation, ClosePayrollMutation, \
    RejectPayrollMutation, MakePaymentForPayrollMutation, CancelPayrollPaymentMutation, \
    CancelPayrollReconciliationMutation, TriggerPayrollReconciliationMutation, \
    DeleteBenefitConsumptionMutation
from payroll.gql_queries import BenefitConsumptionGQLType, PaymentPointGQLType, \
    PayrollGQLType, PaymentMethodGQLType, \
    PaymentMethodListGQLType, BenefitAttachmentListGQLType, \
    CsvReconciliationUploadGQLType, PayrollBenefitConsumptionGQLType, \
    PaymentGatewayConfigGQLType, BenefitsSummaryGQLType
from payroll.mutation_poll_status import PayrollMutationPollStatusGQLType
from payroll.models import PaymentPoint, Payroll, \
    BenefitConsumption, BenefitAttachment, \
    CsvReconciliationUpload, PayrollBenefitConsumption, BenefitConsumptionStatus
from payroll.payments_registry import PaymentMethodStorage
from social_protection.models import BenefitPlan


class PayrollConnectionField(OrderedDjangoFilterConnectionField):
    """Tri par défaut : date_created DESC (plus récent en premier)."""

    @classmethod
    def orderBy(cls, qs, args):
        return qs.order_by("-date_created")


class Query(graphene.ObjectType):
    payment_point = OrderedDjangoFilterConnectionField(
        PaymentPointGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        client_mutation_id=graphene.String(),
        parent_location=graphene.String(),
        parent_location_level=graphene.Int(),
    )
    payroll = PayrollConnectionField(
        PayrollGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        client_mutation_id=graphene.String(),
    )

    bill_by_payroll = OrderedDjangoFilterConnectionField(
        BillGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
        payroll_uuid=graphene.UUID(required=True)
    )

    benefit_consumption = OrderedDjangoFilterConnectionField(
        BenefitConsumptionGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        client_mutation_id=graphene.String(),
    )

    payment_methods = graphene.Field(
        PaymentMethodListGQLType,
    )

    payment_gateway_config = graphene.Field(
        PaymentGatewayConfigGQLType,
    )

    benefit_consumption_by_payroll = OrderedDjangoFilterConnectionField(
        BenefitConsumptionGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
        payroll_uuid=graphene.UUID(required=True),
        filterOnlyUnpaid=graphene.Boolean(),
        benefit_status=graphene.String(
            description=(
                "Filtre serveur sur BenefitConsumption.status "
                "(ex. APPROVE_FOR_PAYMENT). Nommé benefitStatus en GraphQL."
            ),
        ),
    )

    benefit_consumption_status_counts = graphene.JSONString(
        payroll_uuid=graphene.UUID(required=True),
        description="Compteurs par statut {STATUS: n} sans charger les lignes.",
    )

    benefit_attachment_by_payroll = OrderedDjangoFilterConnectionField(
        BenefitAttachmentListGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        applyDefaultValidityFilter=graphene.Boolean(),
        client_mutation_id=graphene.String(),
        payroll_uuid=graphene.UUID(required=True)
    )

    csv_reconciliation_upload = OrderedDjangoFilterConnectionField(
        CsvReconciliationUploadGQLType,
        orderBy=graphene.List(of_type=graphene.String),
    )

    payroll_benefit_consumption = OrderedDjangoFilterConnectionField(
        PayrollBenefitConsumptionGQLType,
        orderBy=graphene.List(of_type=graphene.String),
        dateValidFrom__Gte=graphene.DateTime(),
        dateValidTo__Lte=graphene.DateTime(),
        client_mutation_id=graphene.String(),
        benefitPlanName=graphene.String(),
        benefitPlanUuid=graphene.String(),
        paymentCycleUuid=graphene.String(),
    )

    benefits_summary = graphene.Field(
        BenefitsSummaryGQLType,
        individualId=graphene.String(),
        payrollId=graphene.String(),
        benefitPlanUuid=graphene.String(),
        paymentCycleUuid=graphene.String(),
    )

    payroll_creation_progress = graphene.JSONString(
        payroll_id=graphene.UUID(),
        client_mutation_id=graphene.String(),
    )

    payroll_payment_progress = graphene.JSONString(
        payroll_id=graphene.UUID(required=True),
    )

    payroll_reconciliation_progress = graphene.JSONString(
        payroll_id=graphene.UUID(required=True),
    )

    payroll_mutation_poll_status = graphene.Field(
        PayrollMutationPollStatusGQLType,
        client_mutation_id=graphene.String(required=True),
        payroll_id=graphene.UUID(
            description="Optionnel : état paiement si le journal de mutation est absent.",
        ),
    )

    def resolve_payroll_mutation_poll_status(self, info, client_mutation_id, payroll_id=None):
        from payroll.mutation_poll_status import resolve_payroll_mutation_poll_status

        return resolve_payroll_mutation_poll_status(
            info, client_mutation_id, payroll_id=payroll_id
        )

    def resolve_payroll_payment_progress(self, info, payroll_id):
        from payroll.payment_progress import reconcile_payment_progress_for_payroll

        Query._check_permissions(info.context.user, PayrollConfig.gql_payroll_search_perms)
        payroll = Payroll.objects.filter(id=payroll_id, is_deleted=False).first()
        if not payroll:
            return None
        return reconcile_payment_progress_for_payroll(payroll)

    def resolve_payroll_reconciliation_progress(self, info, payroll_id):
        from payroll.reconciliation_progress import reconcile_reconciliation_progress_for_payroll

        Query._check_permissions(info.context.user, PayrollConfig.gql_payroll_search_perms)
        payroll = Payroll.objects.filter(id=payroll_id, is_deleted=False).first()
        if not payroll:
            return None
        return reconcile_reconciliation_progress_for_payroll(payroll)

    def resolve_payroll_creation_progress(self, info, payroll_id=None, client_mutation_id=None):
        from payroll.creation_progress import (
            get_payroll_creation_progress,
            get_payroll_creation_progress_by_mutation,
        )

        Query._check_permissions(info.context.user, PayrollConfig.gql_payroll_search_perms)
        if client_mutation_id:
            return get_payroll_creation_progress_by_mutation(client_mutation_id)
        if payroll_id:
            return get_payroll_creation_progress(str(payroll_id))
        from payroll.creation_progress import STATUS_IN_PROGRESS

        return {
            "status": STATUS_IN_PROGRESS,
            "should_stop_polling": False,
            "error": None,
            "percent": 0,
        }

    def resolve_bill_by_payroll(self, info, **kwargs):
        Query._check_permissions(info.context.user, PayrollConfig.gql_payroll_search_perms)
        filters = [*append_validity_filter(**kwargs), Q(payrollbill__payroll_id=kwargs.get("payroll_uuid"),
                                                        is_deleted=False,
                                                        payrollbill__is_deleted=False,
                                                        payrollbill__payroll__is_deleted=False)]

        client_mutation_id = kwargs.get("client_mutation_id", None)
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        subject_type = kwargs.get("subject_type", None)
        if subject_type:
            filters.append(Q(subject_type__model=subject_type))

        thirdparty_type = kwargs.get("thirdparty_type", None)
        if thirdparty_type:
            filters.append(Q(thirdparty_type__model=thirdparty_type))

        return gql_optimizer.query(Bill.objects.filter(*filters), info)

    def resolve_benefit_consumption_status_counts(self, info, payroll_uuid):
        from payroll.benefit_consumption_query import benefit_consumption_status_counts_for_payroll

        Query._check_permissions(info.context.user, PayrollConfig.gql_payroll_search_perms)
        return benefit_consumption_status_counts_for_payroll(payroll_uuid)

    def resolve_benefit_consumption_by_payroll(self, info, **kwargs):
        from payroll.benefit_consumption_query import benefit_consumption_queryset_for_payroll

        Query._check_permissions(info.context.user, PayrollConfig.gql_payroll_search_perms)
        payroll_uuid = kwargs.get("payroll_uuid")
        client_mutation_id = kwargs.get("client_mutation_id", None)
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)

        queryset = benefit_consumption_queryset_for_payroll(
            payroll_uuid,
            status=kwargs.get("benefit_status"),
            filter_only_unpaid=kwargs.get("filterOnlyUnpaid"),
        )
        if client_mutation_id:
            queryset = queryset.filter(
                mutations__mutation__client_mutation_id=client_mutation_id
            )
        validity = append_validity_filter(**kwargs)
        if validity:
            queryset = queryset.filter(*validity)

        return gql_optimizer.query(queryset, info)

    def resolve_benefit_attachment_by_payroll(self, info, **kwargs):
        Query._check_permissions(info.context.user, PayrollConfig.gql_payroll_search_perms)
        filters = [*append_validity_filter(**kwargs),
                   Q(benefit__payrollbenefitconsumption__payroll_id=kwargs.get("payroll_uuid"),
                     is_deleted=False,
                     benefit__payrollbenefitconsumption__is_deleted=False,
                     benefit__payrollbenefitconsumption__payroll__is_deleted=False)]

        client_mutation_id = kwargs.get("client_mutation_id", None)
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        return gql_optimizer.query(BenefitAttachment.objects.filter(*filters), info)

    def resolve_payment_point(self, info, **kwargs):
        Query._check_permissions(info.context.user, PayrollConfig.gql_payment_point_search_perms)
        filters = []

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        parent_location = kwargs.get('parent_location')
        if parent_location:
            filters += [get_ancestor_location_filter(parent_location)]

        query = PaymentPoint.objects.filter(*filters)
        return gql_optimizer.query(query, info)

    def resolve_payroll(self, info, **kwargs):
        Query._check_permissions(info.context.user, PayrollConfig.gql_payroll_search_perms)
        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        query = Payroll.objects.filter(*filters).order_by("-date_created")
        return gql_optimizer.query(query, info)

    def resolve_payroll_benefit_consumption(self, info, **kwargs):
        Query._check_permissions(info.context.user, PayrollConfig.gql_payroll_search_perms)
        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        benefit_plan_name = kwargs.get("benefitPlanName")
        if benefit_plan_name:
            benefit_plan_ids = list(BenefitPlan.objects.filter(name__icontains=benefit_plan_name).values_list('id', flat=True))
            filters.append(Q(payroll__payment_plan__benefit_plan_id__in=benefit_plan_ids))

        benefit_plan_uuid = kwargs.get("benefitPlanUuid")
        if benefit_plan_uuid:
            filters.append(Q(payroll__payment_plan__benefit_plan_id=benefit_plan_uuid))

        payment_cycle_uuid = kwargs.get("paymentCycleUuid")
        if payment_cycle_uuid:
            filters.append(Q(payroll__payment_cycle_id=payment_cycle_uuid))

        query = PayrollBenefitConsumption.objects.filter(*filters)
        return gql_optimizer.query(query, info)

    def resolve_benefit_consumption(self, info, **kwargs):
        Query._check_permissions(info.context.user, PayrollConfig.gql_payroll_search_perms)
        filters = append_validity_filter(**kwargs)

        client_mutation_id = kwargs.get("client_mutation_id")
        if client_mutation_id:
            wait_for_mutation(client_mutation_id)
            filters.append(Q(mutations__mutation__client_mutation_id=client_mutation_id))

        query = BenefitConsumption.objects.filter(*filters)
        return gql_optimizer.query(query, info)

    def resolve_payment_methods(self, info, **kwargs):
        user = info.context.user
        if type(user) is AnonymousUser or not user.id:
            raise PermissionError("Unauthorized")

        payment_methods = PaymentMethodStorage.get_all_available_payment_methods()
        gql_payment_methods = Query._build_payment_method_options(payment_methods)
        return PaymentMethodListGQLType(gql_payment_methods)

    def resolve_payment_gateway_config(self, info):
        return PaymentGatewayConfigGQLType(
            base_url=PayrollConfig.payment_gateway_base_url,
            api_key=PayrollConfig.payment_gateway_api_key,
            timeout=PayrollConfig.payment_gateway_timeout,
        )

    def resolve_csv_reconciliation_upload(self, info, **kwargs):
        Query._check_permissions(info.context.user, PayrollConfig.gql_csv_reconciliation_search_perms)
        filters = append_validity_filter(**kwargs)

        query = CsvReconciliationUpload.objects.filter(*filters)
        return gql_optimizer.query(query, info)

    def resolve_benefits_summary(self, info, **kwargs):
        Query._check_permissions(info.context.user,
                                 PayrollConfig.gql_payroll_search_perms)
        filters = append_validity_filter(**kwargs)
        individual_id = kwargs.get("individualId", None)
        payroll_id = kwargs.get("payrollId", None)
        benefit_plan_uuid = kwargs.get("benefitPlanUuid", None)
        payment_cycle_uuid = kwargs.get("paymentCycleUuid", None)

        if individual_id:
            filters.append(Q(individual__id=individual_id))

        if payroll_id:
            filters.append(Q(payrollbenefitconsumption__payroll_id=payroll_id))

        if benefit_plan_uuid:
            filters.append(Q(payrollbenefitconsumption__payroll__payment_plan__benefit_plan_id=benefit_plan_uuid))

        if payment_cycle_uuid:
            filters.append(Q(payrollbenefitconsumption__payroll__payment_cycle_id=payment_cycle_uuid))

        amount_received = BenefitConsumption.objects.filter(
            *filters,
            is_deleted=False,
            payrollbenefitconsumption__is_deleted=False,
            status=BenefitConsumptionStatus.RECONCILED
        ).aggregate(total_received=Sum('amount'))['total_received'] or 0

        amount_due = BenefitConsumption.objects.filter(
            *filters,
            is_deleted=False,
            payrollbenefitconsumption__is_deleted=False
        ).exclude(status=BenefitConsumptionStatus.RECONCILED).aggregate(total_due=Sum('amount'))['total_due'] or 0

        return BenefitsSummaryGQLType(
            total_amount_received=amount_received,
            total_amount_due=amount_due,
        )

    @staticmethod
    def _build_payment_method_options(payment_methods):
        gql_payment_methods = []
        for payment_method in payment_methods:
            gql_payment_methods.append(
                PaymentMethodGQLType(
                    name=payment_method['name']
                )
            )
        return gql_payment_methods

    @staticmethod
    def _check_permissions(user, perms):
        if type(user) is AnonymousUser or not user.id or not user.has_perms(perms):
            raise PermissionError(_("Unauthorized"))


class Mutation(graphene.ObjectType):
    create_payment_point = CreatePaymentPointMutation.Field()
    update_payment_point = UpdatePaymentPointMutation.Field()
    delete_payment_point = DeletePaymentPointMutation.Field()

    create_payroll = CreatePayrollMutation.Field()
    delete_payroll = DeletePayrollMutation.Field()
    close_payroll = ClosePayrollMutation.Field()
    reject_payroll = RejectPayrollMutation.Field()
    make_payment_for_payroll = MakePaymentForPayrollMutation.Field()
    cancel_payroll_payment = CancelPayrollPaymentMutation.Field()
    cancel_payroll_reconciliation = CancelPayrollReconciliationMutation.Field()
    trigger_payroll_reconciliation = TriggerPayrollReconciliationMutation.Field()
    delete_benefit_consumption = DeleteBenefitConsumptionMutation.Field()


from payroll.mutation_log_gql_extension import register_mutation_log_task_bar_fields

register_mutation_log_task_bar_fields()
