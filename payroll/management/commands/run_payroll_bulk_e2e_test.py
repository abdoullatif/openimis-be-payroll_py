"""
Test E2E réel : PayrollService.create avec bulk persist (données conservées).

Usage:
  python manage.py run_payroll_bulk_e2e_test
  python manage.py run_payroll_bulk_e2e_test --payment-plan-code PL004
"""

import time
from datetime import datetime

from django.core.management.base import BaseCommand

from calcrule_social_protection.apps import CalcruleSocialProtectionConfig
from contribution_plan.models import PaymentPlan
from core.models import User
from payment_cycle.models import PaymentCycle
from payroll.creation_progress import get_payroll_creation_progress
from payroll.models import Payroll, PayrollBenefitConsumption, PayrollStatus
from payroll.services import PayrollService
from social_protection.models import Beneficiary, BeneficiaryStatus
from tasks_management.models import Task


class Command(BaseCommand):
    help = "Real end-to-end payroll create test (bulk enabled)"

    def add_arguments(self, parser):
        parser.add_argument("--payment-plan-code", type=str, default="PL002")
        parser.add_argument("--username", type=str, default="E00013")

    def handle(self, *args, **options):
        pp = PaymentPlan.objects.filter(code=options["payment_plan_code"]).first()
        if not pp:
            self.stderr.write(f"Payment plan {options['payment_plan_code']} not found")
            return

        user = User.objects.filter(username=options["username"]).first()
        if not user:
            self.stderr.write(f"User {options['username']} not found")
            return

        pc = PaymentCycle.objects.order_by("-date_created").first()
        expected = Beneficiary.objects.filter(
            benefit_plan=pp.benefit_plan,
            status=BeneficiaryStatus.ACTIVE,
            is_deleted=False,
        ).count()

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        payroll_name = f"BENCH-BULK-E2E-{pp.code}-{stamp}"

        obj_data = {
            "name": payroll_name,
            "payment_plan_id": str(pp.id),
            "payment_cycle_id": str(pc.id),
            "payment_method": "StrategyOnlinePayment",
            "status": PayrollStatus.PENDING_APPROVAL,
            "date_valid_from": str(pc.start_date)[:10] if pc.start_date else None,
            "date_valid_to": str(pc.end_date)[:10] if pc.end_date else None,
            "json_ext": {},
        }

        self.stdout.write(self.style.MIGRATE_HEADING("E2E payroll create test (bulk)"))
        self.stdout.write(f"Plan: {pp.code} — expected beneficiaries: {expected}")
        self.stdout.write(f"Bulk enabled: {getattr(CalcruleSocialProtectionConfig, 'payroll_bulk_persist_enabled', True)}")
        self.stdout.write(f"Payroll name: {payroll_name}")
        self.stdout.write("")

        service = PayrollService(user)
        t0 = time.time()
        result = service.create(obj_data)
        elapsed = time.time() - t0

        if not result.get("success"):
            self.stderr.write(self.style.ERROR(f"Create failed: {result}"))
            return

        payroll_id = result["data"]["id"]
        payroll = Payroll.objects.get(id=payroll_id)
        benefit_count = PayrollBenefitConsumption.objects.filter(
            payroll=payroll, is_deleted=False
        ).count()
        progress_cache = get_payroll_creation_progress(str(payroll.id))
        progress_json = (payroll.json_ext or {}).get("creation_progress")
        task = Task.objects.filter(
            source="payroll", entity_id=payroll.id, is_deleted=False
        ).order_by("-date_created").first()

        self.stdout.write(self.style.SUCCESS(f"Create OK in {elapsed:.2f}s"))
        self.stdout.write(f"  Payroll id: {payroll_id}")
        self.stdout.write(f"  Status: {payroll.status}")
        self.stdout.write(f"  Benefits linked: {benefit_count} (expected {expected})")
        self.stdout.write(f"  creation_progress (cache): {progress_cache}")
        self.stdout.write(f"  creation_progress (json_ext): {progress_json}")
        self.stdout.write(f"  Accept task: {task.id if task else 'MISSING'} source={getattr(task, 'source', None)}")

        if benefit_count != expected:
            self.stdout.write(
                self.style.WARNING(f"  WARNING: benefit count mismatch ({benefit_count} vs {expected})")
            )
        else:
            self.stdout.write(self.style.SUCCESS("  Benefit count OK"))

        self.stdout.write("")
        self.stdout.write("You can verify in UI with payroll name: " + payroll_name)
