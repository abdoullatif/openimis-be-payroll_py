import abc


class StrategyOfPaymentInterface(object,  metaclass=abc.ABCMeta):

    @classmethod
    def initialize_payment_gateway(cls):
        pass

    @classmethod
    def accept_payroll(cls, payroll, user, **kwargs):
        pass

    @classmethod
    def make_payment_for_payroll(cls, payroll, user, **kwargs):
        pass

    @classmethod
    def reject_payroll(cls, payroll, user, **kwargs):
        from payroll.models import PayrollStatus
        cls.change_status_of_payroll(payroll, PayrollStatus.REJECTED, user)
        cls.remove_benefits_from_rejected_payroll(payroll)

    @classmethod
    def reject_approved_payroll(cls, payroll, user):
        from django.contrib.contenttypes.models import ContentType
        from core.services.utils.serviceUtils import model_representation
        from payroll.models import (
            BenefitConsumption,
            BenefitConsumptionStatus,
            PayrollStatus
        )
        from invoice.models import (
            DetailPaymentInvoice,
            PaymentInvoice,
            Bill
        )
        from payroll.services import PayrollService

        benefit_data = BenefitConsumption.objects.filter(
            payrollbenefitconsumption__payroll=payroll,
            status=BenefitConsumptionStatus.RECONCILED,
            is_deleted=False
        )
        benefit_data_related = list(benefit_data.values_list('id', 'benefitattachment__bill'))
        if len(benefit_data_related) > 0:
            benefits, related_bills = zip(*benefit_data_related)
            bill_content_type = ContentType.objects.get_for_model(Bill)
            detail_payment_invoices = DetailPaymentInvoice.objects.filter(
                subject_type=bill_content_type,
                subject_id__in=related_bills
            )
            payment_invoice_ids = list(detail_payment_invoices.values_list('payment_id', flat=True))
            detail_payment_invoices.delete()
            PaymentInvoice.objects.filter(id__in=payment_invoice_ids).delete()

        for benefit in benefit_data:
            benefit.receipt = None
            benefit.status = BenefitConsumptionStatus.ACCEPTED
            benefit.save(username=user.username)
        cls.change_status_of_payroll(payroll, PayrollStatus.PENDING_APPROVAL, user)
        PayrollService(user).create_accept_payroll_task(payroll.id, model_representation(payroll))

    @classmethod
    def acknowledge_of_reponse_view(cls, payroll, response_from_gateway, user, rejected_bills):
        pass

    @classmethod
    def reconcile_payroll(cls, payroll, user):
        pass

    @classmethod
    def change_status_of_payroll(cls, payroll, status, user, opensearch_status_only=False):
        payroll.status = status
        if opensearch_status_only:
            from payroll.opensearch_payroll_status_sync import payroll_status_only_save

            payroll_status_only_save(payroll, user)
        else:
            payroll.save(username=user.login_name)

    @classmethod
    def remove_benefits_from_rejected_payroll(cls, payroll):
        from payroll.models import (
            BenefitAttachment,
            BenefitConsumption,
            PayrollBenefitConsumption,
        )
        from invoice.models import (
            Bill,
            BillItem
        )

        benefit_data = BenefitConsumption.objects.filter(
            payrollbenefitconsumption__payroll=payroll,
            is_deleted=False
        ).values_list('id', 'benefitattachment__bill')

        if len(benefit_data) > 0:
            benefits, related_bills = zip(*benefit_data)

            BenefitAttachment.objects.filter(
                benefit_id__in=benefits
            ).delete()

            BillItem.objects.filter(
                bill__id__in=related_bills
            ).delete()

            Bill.objects.filter(
                id__in=related_bills
            ).delete()

            PayrollBenefitConsumption.objects.filter(payroll=payroll).delete()

            BenefitConsumption.objects.filter(
                id__in=benefits,
                is_deleted=False
            ).delete()

    @classmethod
    def remove_benefit_from_payroll(cls, benefit):
        from payroll.models import (
            BenefitAttachment,
            BenefitConsumption,
            PayrollBenefitConsumption
        )
        from invoice.models import (
            Bill,
            BillItem
        )

        benefit_data = BenefitConsumption.objects.filter(
            id=benefit.id,
            is_deleted=False
        ).values_list('id', 'benefitattachment__bill')

        if len(benefit_data) > 0:
            benefits, related_bills = zip(*benefit_data)

            BenefitAttachment.objects.filter(
                benefit_id__in=benefits
            ).delete()

            BillItem.objects.filter(
                bill__id__in=related_bills
            ).delete()

            Bill.objects.filter(
                id__in=related_bills
            ).delete()

            PayrollBenefitConsumption.objects.filter(benefit=benefit).delete()

            BenefitConsumption.objects.filter(
                id__in=benefits,
                is_deleted=False
            ).delete()
