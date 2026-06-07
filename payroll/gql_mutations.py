import graphene
from gettext import gettext as _
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.db import transaction
import logging

from core.gql.gql_mutations.base_mutation import BaseHistoryModelCreateMutationMixin, BaseMutation, \
    BaseHistoryModelUpdateMutationMixin, BaseHistoryModelDeleteMutationMixin
from core.schema import OpenIMISMutation
from payroll.apps import PayrollConfig
from payroll.models import PaymentPoint, Payroll, PayrollStatus, PayrollMutation
from payroll.services import PaymentPointService, PayrollService, BenefitConsumptionService

logger = logging.getLogger(__name__)


class CreatePaymentPointInputType(OpenIMISMutation.Input):
    name = graphene.String(required=True, max_length=255)
    location_id = graphene.Int(required=True)
    ppm_id = graphene.UUID(required=True)


class UpdatePaymentPointInputType(CreatePaymentPointInputType):
    id = graphene.UUID(required=True)


class DeletePaymentPointInputType(OpenIMISMutation.Input):
    ids = graphene.List(graphene.UUID, required=True)


class UpdatePaymentGatewayConfigInputType(OpenIMISMutation.Input):
    base_url = graphene.String(required=True, max_length=255)
    api_key = graphene.String(required=True, max_length=255)
    timeout = graphene.Int(required=True)


class CreatePayrollInput(OpenIMISMutation.Input):
    class PayrollStatusEnum(graphene.Enum):
        PENDING_APPROVAL = PayrollStatus.PENDING_APPROVAL
        APPROVE_FOR_PAYMENT = PayrollStatus.APPROVE_FOR_PAYMENT
        REJECTED = PayrollStatus.REJECTED
        RECONCILED = PayrollStatus.RECONCILED

    name = graphene.String(required=True, max_length=255)
    payment_plan_id = graphene.UUID(required=True)
    payment_point_id = graphene.UUID(required=False)
    payment_cycle_id = graphene.UUID(required=False)
    status = graphene.Field(PayrollStatusEnum, required=True)
    payment_method = graphene.String(required=True, max_length=255)
    from_failed_invoices_payroll_id = graphene.UUID(required=False)

    date_valid_from = graphene.Date(required=False)
    date_valid_to = graphene.Date(required=False)
    json_ext = graphene.JSONString(required=False)


class DeletePayrollInputType(DeletePaymentPointInputType):
    pass


class ClosePayrollInputType(DeletePaymentPointInputType):
    pass


class CreatePaymentPointMutation(BaseHistoryModelCreateMutationMixin, BaseMutation):
    _mutation_class = "CreatePaymentPointMutation"
    _mutation_module = PayrollConfig.name
    _model = PaymentPoint

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.id or not user.has_perms(
                PayrollConfig.gql_payment_point_create_perms):
            raise ValidationError(_("mutation.authentication_required"))

    @classmethod
    def _mutate(cls, user, **data):
        data.pop('client_mutation_id', None)
        data.pop('client_mutation_label', None)

        service = PaymentPointService(user)
        service.create(data)

    class Input(CreatePaymentPointInputType):
        pass


class UpdatePaymentPointMutation(BaseHistoryModelUpdateMutationMixin, BaseMutation):
    _mutation_class = "UpdatePaymentPointMutation"
    _mutation_module = PayrollConfig.name
    _model = PaymentPoint

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.id or not user.has_perms(
                PayrollConfig.gql_payment_point_update_perms):
            raise ValidationError(_("mutation.authentication_required"))

    @classmethod
    def _mutate(cls, user, **data):
        data.pop('client_mutation_id', None)
        data.pop('client_mutation_label', None)

        service = PaymentPointService(user)
        service.update(data)

    class Input(UpdatePaymentPointInputType):
        pass


class DeletePaymentPointMutation(BaseHistoryModelDeleteMutationMixin, BaseMutation):
    _mutation_class = "DeletePaymentPointMutation"
    _mutation_module = PayrollConfig.name
    _model = PaymentPoint

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.id or not user.has_perms(
                PayrollConfig.gql_payment_point_delete_perms):
            raise ValidationError(_("mutation.authentication_required"))

    @classmethod
    def _mutate(cls, user, **data):
        data.pop('client_mutation_id', None)
        data.pop('client_mutation_label', None)

        service = PaymentPointService(user)

        ids = data.get('ids')
        if ids:
            with transaction.atomic():
                for id in ids:
                    service.delete({'id': id, 'user': user})

    class Input(DeletePaymentPointInputType):
        pass


class CreatePayrollMutation(BaseHistoryModelCreateMutationMixin, BaseMutation):
    _mutation_class = "CreatePayrollMutation"
    _mutation_module = "payroll"
    _model = Payroll

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.has_perms(
                PayrollConfig.gql_payroll_create_perms):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        client_mutation_id = data.get("client_mutation_id")
        service = PayrollService(user)
        response = service.create(data)
        if client_mutation_id and response.get("success"):
            payroll_id = response['data']['id']
            payroll = Payroll.objects.get(id=payroll_id)
            PayrollMutation.object_mutated(
                user, client_mutation_id=client_mutation_id, payroll=payroll
            )
        if not response['success']:
            return response
        return None

    class Input(CreatePayrollInput):
        pass


class DeletePayrollMutation(BaseHistoryModelDeleteMutationMixin, BaseMutation):
    _mutation_class = "DeletePayrollMutation"
    _mutation_module = "payroll"
    _model = Payroll

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.has_perms(
                PayrollConfig.gql_payroll_delete_perms):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = PayrollService(user)
        ids = data.get('ids')
        if ids:
            with transaction.atomic():
                for id in ids:
                    service.delete({'id': id, 'user': user})

    class Input(DeletePayrollInputType):
        pass


class ClosePayrollMutation(BaseHistoryModelDeleteMutationMixin, BaseMutation):
    _mutation_class = "ClosePayrollMutation"
    _mutation_module = "payroll"
    _model = Payroll

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.has_perms(
                PayrollConfig.gql_payroll_delete_perms):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = PayrollService(user)
        ids = data.get('ids')
        if ids:
            with transaction.atomic():
                for id in ids:
                    service.close_payroll({'id': id})

    class Input(DeletePayrollInputType):
        pass


class TriggerPayrollReconciliationMutation(BaseHistoryModelDeleteMutationMixin, BaseMutation):
    _mutation_class = "TriggerPayrollReconciliationMutation"
    _mutation_module = "payroll"
    _model = Payroll

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(PayrollConfig.gql_payroll_create_perms):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = PayrollService(user)
        ids = data.get('ids')
        if ids:
            with transaction.atomic():
                for id in ids:
                    service.trigger_payroll_reconciliation({'id': id})

    class Input(DeletePayrollInputType):
        pass


class CancelPayrollPaymentMutation(BaseHistoryModelDeleteMutationMixin, BaseMutation):
    _mutation_class = "CancelPayrollPaymentMutation"
    _mutation_module = "payroll"
    _model = Payroll

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(PayrollConfig.gql_payroll_create_perms):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop("client_mutation_id")
        if "client_mutation_label" in data:
            data.pop("client_mutation_label")

        service = PayrollService(user)
        ids = data.get("ids")
        reason = data.get("reason")
        if ids:
            with transaction.atomic():
                for payroll_id in ids:
                    payload = {"id": payroll_id}
                    if reason:
                        payload["reason"] = reason
                    service.cancel_payment_for_payroll(payload)

    class Input(DeletePayrollInputType):
        reason = graphene.String(required=False)


class CancelPayrollReconciliationMutation(BaseHistoryModelDeleteMutationMixin, BaseMutation):
    _mutation_class = "CancelPayrollReconciliationMutation"
    _mutation_module = "payroll"
    _model = Payroll

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(PayrollConfig.gql_payroll_create_perms):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop("client_mutation_id")
        if "client_mutation_label" in data:
            data.pop("client_mutation_label")

        service = PayrollService(user)
        ids = data.get("ids")
        reason = data.get("reason")
        if ids:
            with transaction.atomic():
                for payroll_id in ids:
                    payload = {"id": payroll_id}
                    if reason:
                        payload["reason"] = reason
                    service.cancel_reconciliation_for_payroll(payload)

    class Input(DeletePayrollInputType):
        reason = graphene.String(required=False)


class MakePaymentForPayrollMutation(BaseHistoryModelDeleteMutationMixin, BaseMutation):
    _mutation_class = "MakePaymentForPayrollMutation"
    _mutation_module = "payroll"
    _model = Payroll

    @classmethod
    def _validate_mutation(cls, user, **data):
        super()._validate_mutation(user, **data)
        if not user.has_perms(PayrollConfig.gql_payroll_create_perms):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        client_mutation_id = data.get("client_mutation_id")
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = PayrollService(user)
        ids = data.get('ids')
        logger.info(
            "[gql] makePaymentForPayroll request ids=%s clientMutationId=%s user=%s",
            ids,
            client_mutation_id,
            getattr(user, "username", None),
        )
        if ids:
            with transaction.atomic():
                for id in ids:
                    service.make_payment_for_payroll({'id': id})

    class Input(DeletePayrollInputType):
        pass


class RejectPayrollMutation(BaseHistoryModelDeleteMutationMixin, BaseMutation):
    _mutation_class = "RejectPayrollMutation"
    _mutation_module = "payroll"
    _model = Payroll

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.has_perms(
                PayrollConfig.gql_payroll_delete_perms):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = PayrollService(user)
        ids = data.get('ids')
        if ids:
            with transaction.atomic():
                for id in ids:
                    service.reject_approved_payroll({'id': id})

    class Input(DeletePayrollInputType):
        pass


class DeleteBenefitConsumptionMutation(BaseHistoryModelDeleteMutationMixin, BaseMutation):
    _mutation_class = "DeleteBenefitConsumptionMutation"
    _mutation_module = "payroll"
    _model = Payroll

    @classmethod
    def _validate_mutation(cls, user, **data):
        if type(user) is AnonymousUser or not user.has_perms(
                PayrollConfig.gql_payroll_delete_perms):
            raise ValidationError("mutation.authentication_required")

    @classmethod
    def _mutate(cls, user, **data):
        if "client_mutation_id" in data:
            data.pop('client_mutation_id')
        if "client_mutation_label" in data:
            data.pop('client_mutation_label')

        service = BenefitConsumptionService(user)
        ids = data.get('ids')
        if ids:
            with transaction.atomic():
                for id in ids:
                    service.delete({'id': id})

    class Input(DeletePayrollInputType):
        pass

