from django.core.management.base import BaseCommand

from payroll.models import BenefitAttachment, PayrollBenefitConsumption
from payroll.documents import BenefitAttachmentDocument


class Command(BaseCommand):
    help = (
        "Imports benefit attachment data from openIMIS into OpenSearch. "
        "Run with: python manage.py add_benefit_attachment_data_to_opensearch."
    )

    def handle(self, *args, **options):
        BenefitAttachmentDocument.init(index="benefit_attachment")

        for attachment in BenefitAttachment.objects.select_related(
            "benefit",
            "benefit__individual",
            "bill",
        ):
            benefit = attachment.benefit
            bill = attachment.bill

            payrolls = PayrollBenefitConsumption.objects.filter(
                benefit=benefit
            ).select_related("payroll", "payroll__payment_plan", "payroll__payment_cycle")

            payroll_data = []
            payment_plan_codes = []
            payment_cycle_codes = []
            payroll_names = []
            for item in payrolls:
                payroll = item.payroll
                if payroll and payroll.payment_plan and payroll.payment_plan.code:
                    payment_plan_codes.append(payroll.payment_plan.code)
                if payroll and payroll.payment_cycle and payroll.payment_cycle.code:
                    payment_cycle_codes.append(payroll.payment_cycle.code)
                if payroll and payroll.name:
                    payroll_names.append(payroll.name)
                payroll_data.append({
                    "name": payroll.name,
                    "status": payroll.status,
                    "payment_method": payroll.payment_method,
                    "date_created": str(payroll.date_created) if payroll.date_created else None,
                    "payment_plan": {
                        "code": payroll.payment_plan.code,
                        "name": payroll.payment_plan.name,
                    } if payroll.payment_plan else None,
                    "payment_cycle": {
                        "code": payroll.payment_cycle.code,
                        "status": payroll.payment_cycle.status,
                        "start_date": payroll.payment_cycle.start_date,
                        "end_date": payroll.payment_cycle.end_date,
                    } if payroll.payment_cycle else None,
                })

            location_data = BenefitAttachmentDocument().prepare_location(attachment)

            benefit_data = {
                "code": benefit.code,
                "status": benefit.status,
                "type": benefit.type,
                "receipt": benefit.receipt,
                "amount": str(benefit.amount) if benefit.amount is not None else None,
                "photo": benefit.photo,
                "date_due": str(benefit.date_due) if benefit.date_due else None,
                "individual": {
                    "first_name": benefit.individual.first_name,
                    "last_name": benefit.individual.last_name,
                    "dob": benefit.individual.dob,
                    "gender": (benefit.individual.json_ext or {}).get("gender"),
                },
            }

            bill_data = {
                "code": bill.code,
                "code_ext": bill.code_ext,
                "code_tp": bill.code_tp,
                "status": bill.status,
                "currency_code": bill.currency_code,
                "note": bill.note,
                "terms": bill.terms,
                "date_created": str(bill.date_created) if bill.date_created else None,
                "date_due": str(bill.date_due) if bill.date_due else None,
                "date_payed": str(bill.date_payed) if bill.date_payed else None,
                "amount_total": float(bill.amount_total) if bill.amount_total is not None else None,
            }

            document = BenefitAttachmentDocument(
                meta={"id": attachment.id},
                bill=bill_data,
                benefit=benefit_data,
                payroll=payroll_data,
                location=location_data,
                payment_plan_codes=payment_plan_codes,
                payment_cycle_codes=payment_cycle_codes,
                payroll_names=payroll_names,
                id=attachment.id,
            )
            result = document.save()
            self.stdout.write(self.style.SUCCESS(result))
