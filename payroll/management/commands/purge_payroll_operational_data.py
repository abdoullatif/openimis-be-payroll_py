"""
Purge sécurisée des données opérationnelles paie (prod test) :

- plans de paiement
- paies (tous statuts : attente, approuvée, réconciliée, rejetée)
- benefit consumptions / factures liées
- payment invoices
- tâches maker-checker liées (paie / plan de paiement)

PRÉSERVÉ : régimes (BenefitPlan), bénéficiaires, individus, cycles de paiement, points de paiement.

Usage (dry-run par défaut) :
  python manage.py purge_payroll_operational_data

Exécution réelle :
  python manage.py purge_payroll_operational_data --execute --confirm PURGE_PROD
"""

from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q

from contribution_plan.apps import ContributionPlanConfig
from contribution_plan.models import PaymentPlan
from core.models import MutationLog, User
from invoice.models import (
    Bill,
    BillEvent,
    BillItem,
    BillPayment,
    DetailPaymentInvoice,
    PaymentInvoice,
)
from payroll.apps import PayrollConfig
from payroll.models import (
    BenefitAttachment,
    BenefitConsumption,
    CsvReconciliationUpload,
    PaymentAdaptorHistory,
    PaymentReport,
    Payroll,
    PayrollBenefitConsumption,
    PayrollBill,
    PayrollMutation,
)
from payroll.reconciliation_lock import (
    set_payment_in_progress,
    set_reconciliation_in_progress,
)
from tasks_management.models import Task, TaskMutation


CONFIRM_TOKEN = "PURGE_PROD"

_TASK_SOURCE_PREFIXES = ("payroll", "contribution_plan")
_TASK_EVENT_PREFIXES = ("payroll.", "contribution_plan.")


def _delete_with_history(model, queryset):
    """Suppression physique + lignes simple-history associées."""
    ids = list(queryset.values_list("id", flat=True))
    if not ids:
        return 0
    if hasattr(model, "history"):
        model.history.model.objects.filter(id__in=ids).delete()
    deleted, _ = queryset.filter(id__in=ids).delete()
    return deleted


def _delete_plain(queryset):
    if not queryset.exists():
        return 0
    deleted, _ = queryset.delete()
    return deleted


def _task_queryset():
    filters = Q()
    for prefix in _TASK_SOURCE_PREFIXES:
        filters |= Q(source__startswith=prefix)
    for prefix in _TASK_EVENT_PREFIXES:
        filters |= Q(business_event__startswith=prefix)
    for event in (
        PayrollConfig.payroll_accept_event,
        PayrollConfig.payroll_reconciliation_event,
        PayrollConfig.payroll_reject_event,
        PayrollConfig.payroll_delete_event,
        PayrollConfig.benefit_delete_event,
        ContributionPlanConfig.payment_plan_create_event,
        ContributionPlanConfig.payment_plan_update_event,
        ContributionPlanConfig.payment_plan_delete_event,
    ):
        if event:
            filters |= Q(business_event=event)
    payroll_ct = ContentType.objects.get_for_model(Payroll)
    payment_plan_ct = ContentType.objects.get_for_model(PaymentPlan)
    filters |= Q(entity_type=payroll_ct) | Q(entity_type=payment_plan_ct)
    return Task.objects.filter(filters)


def _clear_payroll_redis_keys(stdout, style):
    """Best-effort : clés cache progression paie."""
    cleared = 0
    try:
        from django_redis import get_redis_connection
    except ImportError:
        stdout.write(style.WARNING("django_redis indisponible — skip cache Redis"))
        return 0
    try:
        conn = get_redis_connection("default")
        for pattern in ("*payroll_creation*", "*payroll_payment*", "*payroll_reconciliation*"):
            for key in conn.scan_iter(match=pattern, count=500):
                conn.delete(key)
                cleared += 1
    except Exception as exc:
        stdout.write(style.WARNING(f"Nettoyage Redis partiel : {exc}"))
    return cleared


class Command(BaseCommand):
    help = (
        "Supprime plans de paiement, paies, factures/benefits de paie, paiements et tâches "
        "associées. Les régimes et bénéficiaires ne sont pas touchés."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--execute",
            action="store_true",
            help="Appliquer les suppressions (sinon simulation / dry-run).",
        )
        parser.add_argument(
            "--confirm",
            default="",
            help=f"Obligatoire avec --execute : valeur exacte {CONFIRM_TOKEN}",
        )
        parser.add_argument(
            "--username",
            default="Admin",
            help="Utilisateur pour déverrouiller payment/reconciliation flags sur les paies.",
        )
        parser.add_argument(
            "--skip-opensearch",
            action="store_true",
            help="Ne pas tenter de supprimer les documents OpenSearch.",
        )
        parser.add_argument(
            "--skip-redis",
            action="store_true",
            help="Ne pas nettoyer les clés Redis de progression.",
        )

    def handle(self, *args, **options):
        execute = options["execute"]
        if execute and options["confirm"] != CONFIRM_TOKEN:
            raise CommandError(
                f"--execute requiert --confirm {CONFIRM_TOKEN}"
            )

        user = User.objects.filter(username=options["username"]).first()
        if execute and not user:
            raise CommandError(f"Utilisateur '{options['username']}' introuvable.")

        stats = self._collect_stats()
        self._print_plan(stats)

        if not execute:
            self.stdout.write(
                self.style.WARNING(
                    "\nDry-run : aucune suppression. "
                    f"Ajoutez --execute --confirm {CONFIRM_TOKEN} pour appliquer."
                )
            )
            return

        with transaction.atomic():
            self._unlock_payrolls(user)
            deleted = self._purge(stats)
        if not options["skip_redis"]:
            n = _clear_payroll_redis_keys(self.stdout, self.style)
            self.stdout.write(self.style.SUCCESS(f"Clés Redis supprimées : {n}"))
        if not options["skip_opensearch"]:
            self._purge_opensearch(stats)
        self.stdout.write(self.style.SUCCESS(f"\nPurge terminée. Lignes supprimées : {deleted}"))

    def _collect_stats(self):
        payroll_ids = list(Payroll.objects.values_list("id", flat=True))
        benefit_ids = list(
            PayrollBenefitConsumption.objects.filter(payroll_id__in=payroll_ids).values_list(
                "benefit_id", flat=True
            )
        )
        bill_ids_from_benefits = list(
            BenefitAttachment.objects.filter(benefit_id__in=benefit_ids).values_list(
                "bill_id", flat=True
            )
        )
        bill_ids_from_payroll = list(
            PayrollBill.objects.filter(payroll_id__in=payroll_ids).values_list("bill_id", flat=True)
        )
        bill_ids = list(set(bill_ids_from_benefits) | set(bill_ids_from_payroll))
        payment_plan_ids = list(PaymentPlan.objects.values_list("id", flat=True))
        task_ids = list(_task_queryset().values_list("id", flat=True))
        return {
            "payroll_ids": payroll_ids,
            "benefit_ids": benefit_ids,
            "bill_ids": bill_ids,
            "payment_plan_ids": payment_plan_ids,
            "task_ids": task_ids,
        }

    def _print_plan(self, stats):
        self.stdout.write(self.style.MIGRATE_HEADING("=== Purge opérationnelle paie ==="))
        self.stdout.write(f"Paies                      : {len(stats['payroll_ids'])}")
        self.stdout.write(f"Benefit consumptions       : {len(stats['benefit_ids'])}")
        self.stdout.write(f"Factures (Bill)            : {len(stats['bill_ids'])}")
        self.stdout.write(f"Plans de paiement          : {len(stats['payment_plan_ids'])}")
        self.stdout.write(f"Tâches liées               : {len(stats['task_ids'])}")
        self.stdout.write(
            self.style.NOTICE(
                "\nConservé : BenefitPlan, Beneficiary, Individual, PaymentCycle, PaymentPoint"
            )
        )

    def _unlock_payrolls(self, user):
        for payroll in Payroll.objects.all().iterator():
            set_payment_in_progress(payroll, user, False)
            set_reconciliation_in_progress(payroll, user, False)

    def _purge(self, stats):
        total = 0
        payroll_ids = stats["payroll_ids"]
        benefit_ids = stats["benefit_ids"]
        bill_ids = stats["bill_ids"]
        task_ids = stats["task_ids"]
        payment_plan_ids = stats["payment_plan_ids"]

        # --- Tâches (avant entités référencées) ---
        total += _delete_plain(TaskMutation.objects.filter(task_id__in=task_ids))
        total += _delete_with_history(Task, Task.objects.filter(id__in=task_ids))

        # --- Mutations paie ---
        mutation_ids = list(
            PayrollMutation.objects.filter(payroll_id__in=payroll_ids).values_list(
                "mutation_id", flat=True
            )
        )
        total += _delete_plain(PayrollMutation.objects.filter(payroll_id__in=payroll_ids))

        # --- Paiements / réconciliation factures ---
        from invoice.models import BillMutation, BillItemMutation, BillPaymentMutation, BillEventMutation
        from invoice.models import PaymentInvoiceMutation, DetailPaymentInvoiceMutation

        bill_ct = ContentType.objects.get_for_model(Bill)
        detail_qs = DetailPaymentInvoice.objects.filter(
            Q(subject_type=bill_ct, subject_id__in=[str(b) for b in bill_ids])
            | Q(subject_type=bill_ct, subject_id__in=bill_ids)
        )
        payment_invoice_ids = list(detail_qs.values_list("payment_id", flat=True).distinct())

        total += _delete_plain(DetailPaymentInvoiceMutation.objects.filter(detail_payment_invoice__in=detail_qs))
        total += _delete_plain(detail_qs)
        total += _delete_plain(PaymentInvoiceMutation.objects.filter(payment_invoice_id__in=payment_invoice_ids))
        total += _delete_with_history(PaymentInvoice, PaymentInvoice.objects.filter(id__in=payment_invoice_ids))

        # --- Factures ---
        total += _delete_plain(BillEventMutation.objects.filter(bill_event__bill_id__in=bill_ids))
        total += _delete_plain(BillItemMutation.objects.filter(bile_items__bill_id__in=bill_ids))
        total += _delete_plain(BillPaymentMutation.objects.filter(bill_payment__bill_id__in=bill_ids))
        total += _delete_plain(BillMutation.objects.filter(bill_id__in=bill_ids))
        total += _delete_plain(BillEvent.objects.filter(bill_id__in=bill_ids))
        total += _delete_plain(BillPayment.objects.filter(bill_id__in=bill_ids))
        total += _delete_plain(BillItem.objects.filter(bill_id__in=bill_ids))

        # --- Liens paie ---
        total += _delete_plain(CsvReconciliationUpload.objects.filter(payroll_id__in=payroll_ids))
        total += _delete_plain(PaymentReport.objects.filter(payroll_id__in=payroll_ids))
        total += _delete_plain(PaymentAdaptorHistory.objects.filter(payroll_id__in=payroll_ids))
        total += _delete_plain(PayrollBill.objects.filter(payroll_id__in=payroll_ids))
        total += _delete_plain(BenefitAttachment.objects.filter(benefit_id__in=benefit_ids))
        total += _delete_plain(PayrollBenefitConsumption.objects.filter(payroll_id__in=payroll_ids))

        # --- Benefits créés par la paie ---
        total += _delete_with_history(BenefitConsumption, BenefitConsumption.objects.filter(id__in=benefit_ids))

        # --- Factures ---
        total += _delete_with_history(Bill, Bill.objects.filter(id__in=bill_ids))

        # --- Paies ---
        total += _delete_with_history(Payroll, Payroll.objects.filter(id__in=payroll_ids))

        # --- Plans de paiement ---
        total += _delete_with_history(PaymentPlan, PaymentPlan.objects.filter(id__in=payment_plan_ids))

        # --- MutationLog orphelins (paie) ---
        if mutation_ids:
            total += _delete_plain(MutationLog.objects.filter(id__in=mutation_ids))

        return total

    def _purge_opensearch(self, stats):
        try:
            from payroll.documents import PayrollDocument
        except ImportError:
            self.stdout.write(self.style.WARNING("OpenSearch documents indisponibles — skip"))
            return
        try:
            payroll_doc = PayrollDocument()
            for payroll_id in stats["payroll_ids"]:
                try:
                    payroll_doc.update(payroll_id, action="delete", refresh=True)
                except Exception:
                    pass
            self.stdout.write(self.style.SUCCESS("OpenSearch : suppression best-effort effectuée"))
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f"OpenSearch : {exc}"))
