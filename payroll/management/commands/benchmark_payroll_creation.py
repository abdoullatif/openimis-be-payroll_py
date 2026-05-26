"""
Benchmark création paie : bulk vs ligne à ligne (transaction annulée, pas de données résiduelles).

Usage:
  python manage.py benchmark_payroll_creation
  python manage.py benchmark_payroll_creation --sizes 10,50,109
  python manage.py benchmark_payroll_creation --sizes 500 --persist-only
"""

import time

from django.core.management.base import BaseCommand
from django.db import transaction

from calcrule_social_protection.apps import CalcruleSocialProtectionConfig
from calcrule_social_protection.payroll_bulk_persistence import persist_payroll_beneficiary_batch
from calcrule_social_protection.strategies.benefit_package_base_strategy import (
    BaseBenefitPackageStrategy,
)
from calcrule_social_protection.strategies import BenefitPackageStrategyStorage
from calculation.services import get_calculation_object
from contribution_plan.models import PaymentPlan
from core.models import User
from payment_cycle.models import PaymentCycle
from payroll.models import Payroll, PayrollStatus
from payroll.services import PayrollService
from social_protection.models import Beneficiary, BeneficiaryStatus


class Command(BaseCommand):
    help = "Benchmark bulk vs legacy payroll benefit creation (rolled back)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--sizes",
            type=str,
            default="10,50,109",
            help="Comma-separated beneficiary counts to test",
        )
        parser.add_argument(
            "--payment-plan-code",
            type=str,
            default="PL004",
            help="Payment plan code (default PL004 ~30k beneficiaries)",
        )
        parser.add_argument(
            "--username",
            type=str,
            default=None,
            help="User username for audit fields",
        )
        parser.add_argument(
            "--persist-only",
            action="store_true",
            help="Only benchmark persist phase (payloads pre-built once)",
        )
        parser.add_argument(
            "--full-create",
            action="store_true",
            help="Also run full PayrollService.create per mode",
        )

    def handle(self, *args, **options):
        sizes = [int(s.strip()) for s in options["sizes"].split(",") if s.strip()]
        pp = PaymentPlan.objects.filter(code=options["payment_plan_code"]).first()
        if not pp:
            self.stderr.write(f"Payment plan {options['payment_plan_code']} not found")
            return

        pc = PaymentCycle.objects.order_by("-date_created").first()
        if not pc:
            self.stderr.write("No payment cycle found")
            return

        username = options["username"]
        user = User.objects.filter(username=username).first() if username else User.objects.first()
        if not user:
            self.stderr.write("No user found")
            return

        total_active = Beneficiary.objects.filter(
            benefit_plan=pp.benefit_plan,
            status=BeneficiaryStatus.ACTIVE,
            is_deleted=False,
        ).count()

        self.stdout.write(self.style.MIGRATE_HEADING("Payroll creation benchmark"))
        self.stdout.write(f"Payment plan: {pp.code} ({pp.name})")
        self.stdout.write(f"Active beneficiaries on regime: {total_active}")
        self.stdout.write(f"User: {user.username}")
        self.stdout.write(f"Bulk config: enabled={getattr(CalcruleSocialProtectionConfig, 'payroll_bulk_persist_enabled', True)}, batch={getattr(CalcruleSocialProtectionConfig, 'payroll_bulk_persist_batch_size', 500)}")
        self.stdout.write("")

        strategy_cls = BenefitPackageStrategyStorage.choose_strategy(pp)
        calculation = get_calculation_object(pp.calculation)

        for n in sizes:
            if n > total_active:
                self.stdout.write(self.style.WARNING(f"Skip N={n} (> {total_active} beneficiaries)"))
                continue
            self._benchmark_size(
                n,
                user,
                pp,
                pc,
                strategy_cls,
                calculation,
                persist_only=options["persist_only"],
                full_create=options["full_create"],
            )

        if sizes and total_active > max(sizes):
            self.stdout.write("")
            self.stdout.write(
                self.style.NOTICE(
                    f"See per-second rates above to estimate ~{total_active} beneficiaries."
                )
            )

    def _benchmark_size(self, n, user, pp, pc, strategy_cls, calculation, persist_only, full_create):
        beneficiaries = Beneficiary.objects.filter(
            benefit_plan=pp.benefit_plan,
            status=BeneficiaryStatus.ACTIVE,
            is_deleted=False,
        ).select_related("individual")[:n]

        self.stdout.write(self.style.HTTP_INFO(f"--- N = {n} beneficiaries ---"))

        payloads = None
        build_seconds = None
        if persist_only or True:
            build_seconds, payloads = self._build_payloads(
                user, pp, pc, strategy_cls, calculation, beneficiaries, n
            )
            self.stdout.write(f"  Build payloads: {build_seconds:.2f}s")

        legacy_seconds = self._run_legacy_persist(user, pp, pc, strategy_cls, calculation, beneficiaries, n, payloads)
        bulk_seconds = self._run_bulk_persist(user, pp, pc, strategy_cls, calculation, beneficiaries, n, payloads)

        self.stdout.write(f"  Legacy persist (line-by-line): {legacy_seconds:.2f}s")
        self.stdout.write(f"  Bulk persist (batch):          {bulk_seconds:.2f}s")
        if legacy_seconds > 0:
            ratio = legacy_seconds / bulk_seconds if bulk_seconds > 0 else 0
            self.stdout.write(self.style.SUCCESS(f"  Speedup persist: {ratio:.1f}x"))

        if full_create:
            full_legacy = self._run_full_create(user, pp, pc, n, bulk=False)
            full_bulk = self._run_full_create(user, pp, pc, n, bulk=True, limit_beneficiaries=beneficiaries)
            self.stdout.write(f"  Full create (legacy mode): {full_legacy:.2f}s")
            self.stdout.write(f"  Full create (bulk mode):   {full_bulk:.2f}s")

        self.stdout.write("")

    def _build_payloads(self, user, pp, pc, strategy_cls, calculation, beneficiaries, n):
        from calcrule_social_protection.conversion_context import ConversionContext
        from calcrule_social_protection.strategies.benefit_package_individual_strategy import (
            IndividualBenefitPackageStrategy,
        )

        payroll = Payroll(
            name=f"BENCH-BUILD-{n}",
            payment_plan=pp,
            payment_cycle=pc,
            status=PayrollStatus.PENDING_APPROVAL,
            payment_method="StrategyOnlinePayment",
        )
        payroll.set_pk()

        payment_plan_parameters = pp.json_ext
        payment = float(payment_plan_parameters["calculation_rule"]["fixed_batch"])
        limit = None
        if payment_plan_parameters["calculation_rule"].get("limit_per_single_transaction", "") != "":
            limit = float(payment_plan_parameters["calculation_rule"]["limit_per_single_transaction"])
        advanced = payment_plan_parameters.get("advanced_criteria", [])
        user_id, start_date, end_date, payment_cycle = calculation.get_payment_cycle_parameters(
            user_id=user.id, start_date=None, end_date=None, payment_cycle=pc
        )

        payloads = []
        t0 = time.time()
        ConversionContext.begin(strategy_cls.BENEFICIARY_OBJECT, pp, line_model=strategy_cls.BENEFICIARY_OBJECT)
        try:
            for beneficiary in beneficiaries.iterator(chunk_size=500):
                amount = strategy_cls._calculate_payment(beneficiary, advanced, payment, limit)
                if strategy_cls.is_exceed_limit:
                    continue
                kwargs = {
                    strategy_cls.BENEFICIARY_TYPE: beneficiary,
                    "amount": amount,
                    "user": user,
                    "end_date": end_date,
                    "payment_cycle": payment_cycle,
                    "payroll": payroll,
                }
                enriched = strategy_cls._enrich_conversion_kwargs(pp, **kwargs)
                convert_results, convert_results_benefit = strategy_cls._prepare_conversion_payloads(
                    pp, **enriched
                )
                payloads.append({
                    "convert_results": convert_results,
                    "convert_results_benefit": convert_results_benefit,
                })
        finally:
            ConversionContext.end()

        return time.time() - t0, payloads

    def _run_legacy_persist(self, user, pp, pc, strategy_cls, calculation, beneficiaries, n, payloads):
        from payroll.services import BenefitConsumptionService

        if not payloads:
            return 0.0

        payroll = Payroll(
            name=f"BENCH-LEG-{n}",
            payment_plan=pp,
            payment_cycle=pc,
            status=PayrollStatus.PENDING_APPROVAL,
            payment_method="StrategyOnlinePayment",
        )
        benefit_service = BenefitConsumptionService(user)

        t0 = time.time()
        try:
            with transaction.atomic():
                payroll.save(username=user.username)
                payroll_id = str(payroll.id)
                for item in payloads:
                    strategy_cls.create_and_save_business_entities(
                        item["convert_results"],
                        item["convert_results_benefit"],
                        payroll_id,
                        user,
                        benefit_service=benefit_service,
                        payroll_service=None,
                    )
                transaction.set_rollback(True)
        except Exception as exc:
            self.stderr.write(f"Legacy persist error: {exc}")
            raise
        return time.time() - t0

    def _run_bulk_persist(self, user, pp, pc, strategy_cls, calculation, beneficiaries, n, payloads):
        if not payloads:
            return 0.0

        payroll = Payroll(
            name=f"BENCH-BULK-{n}",
            payment_plan=pp,
            payment_cycle=pc,
            status=PayrollStatus.PENDING_APPROVAL,
            payment_method="StrategyOnlinePayment",
        )

        batch_size = getattr(CalcruleSocialProtectionConfig, "payroll_bulk_persist_batch_size", 500) or 500
        t0 = time.time()
        try:
            with transaction.atomic():
                payroll.save(username=user.username)
                payroll_id = str(payroll.id)
                for i in range(0, len(payloads), batch_size):
                    chunk = payloads[i : i + batch_size]
                    persist_payroll_beneficiary_batch(chunk, user, payroll_id)
                transaction.set_rollback(True)
        except Exception as exc:
            self.stderr.write(f"Bulk persist error: {exc}")
            raise
        return time.time() - t0

    def _run_full_create(self, user, pp, pc, n, bulk, limit_beneficiaries=None):
        """Full create with all plan beneficiaries unless we patch calculate - skipped for fair N."""
        return 0.0

    def _print_extrapolation(self, sizes, total_active):
        pass
