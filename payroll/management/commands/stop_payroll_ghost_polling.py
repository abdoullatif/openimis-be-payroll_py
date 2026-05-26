"""Arrête les indicateurs de polling fantôme (paiement / réconciliation) sur les paies."""

from django.core.management.base import BaseCommand

from core.models import User
from payroll.models import Payroll
from payroll.payment_progress import (
    cancel_payroll_payment_progress,
    get_payroll_payment_progress,
    persist_payment_progress_to_payroll,
)
from payroll.reconciliation_lock import (
    is_payment_in_progress,
    is_reconciliation_in_progress,
    set_payment_in_progress,
    set_reconciliation_in_progress,
)
from payroll.reconciliation_progress import (
    cancel_payroll_reconciliation_progress,
    get_payroll_reconciliation_progress,
    persist_reconciliation_progress_to_payroll,
)
from payroll.mutation_log_task_bar import (
    close_completed_creation_mutations,
    close_stale_payroll_mutations_for_payroll,
)


class Command(BaseCommand):
    help = (
        "Remet payment_in_progress / reconciliation_in_progress et les caches "
        "de progression en état terminal (CANCELLED) pour arrêter le polling front."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--payroll-id",
            action="append",
            dest="payroll_ids",
            help="UUID paie (répétable). Sans argument : toutes les paies avec indicateur actif.",
        )
        parser.add_argument(
            "--reason",
            default="Ghost polling cleanup.",
            help="Message renvoyé au front dans payment/reconciliation progress.",
        )
        parser.add_argument(
            "--username",
            default="System",
            help="Utilisateur pour save payroll (json_ext).",
        )

    def handle(self, *args, **options):
        reason = options["reason"]
        username = options["username"]
        user = User.objects.filter(username=username).first()
        if not user:
            user = User.objects.order_by("id").first()

        creation_closed = close_completed_creation_mutations(username=username)
        if creation_closed:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Closed {creation_closed} completed payroll creation mutation log(s)."
                )
            )

        payroll_ids = options.get("payroll_ids")
        if payroll_ids:
            payrolls = Payroll.objects.filter(id__in=payroll_ids, is_deleted=False)
        else:
            payrolls = Payroll.objects.filter(is_deleted=False)

        stopped = 0
        for payroll in payrolls:
            payroll_id = str(payroll.id)
            pay_flag = is_payment_in_progress(payroll)
            rec_flag = is_reconciliation_in_progress(payroll)
            pay_cache = (get_payroll_payment_progress(payroll_id) or {}).get("status")
            rec_cache = (get_payroll_reconciliation_progress(payroll_id) or {}).get("status")
            jx = payroll.json_ext or {}
            pay_jx = (jx.get("payment_progress") or {}).get("status") if isinstance(
                jx.get("payment_progress"), dict
            ) else None
            rec_jx = (jx.get("reconciliation_progress") or {}).get("status") if isinstance(
                jx.get("reconciliation_progress"), dict
            ) else None

            needs_payment = (
                pay_flag
                or pay_cache == "IN_PROGRESS"
                or pay_jx == "IN_PROGRESS"
            )
            needs_reconciliation = (
                rec_flag
                or rec_cache == "IN_PROGRESS"
                or rec_jx == "IN_PROGRESS"
            )
            if not needs_payment and not needs_reconciliation:
                continue

            if needs_payment:
                set_payment_in_progress(payroll, user, False)
                progress = cancel_payroll_payment_progress(payroll_id, reason=reason)
                persist_payment_progress_to_payroll(payroll, progress, username)
                self.stdout.write(
                    self.style.SUCCESS(f"Payment polling stopped: {payroll.name} ({payroll_id})")
                )

            if needs_reconciliation:
                set_reconciliation_in_progress(payroll, user, False)
                progress = cancel_payroll_reconciliation_progress(payroll_id, reason=reason)
                persist_reconciliation_progress_to_payroll(payroll, progress, username)
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Reconciliation polling stopped: {payroll.name} ({payroll_id})"
                    )
                )

            closed = close_stale_payroll_mutations_for_payroll(
                payroll_id, reason=reason
            )
            if closed:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Closed {closed} stale mutation log(s) for {payroll.name}"
                    )
                )

            stopped += 1

        if stopped == 0:
            self.stdout.write("No ghost polling indicators found.")
        else:
            self.stdout.write(self.style.SUCCESS(f"Stopped ghost polling on {stopped} payroll(s)."))
