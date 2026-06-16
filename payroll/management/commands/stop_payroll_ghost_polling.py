"""Arrête les indicateurs de polling fantôme (création / indexation / paiement / réconciliation)."""

from django.core.management.base import BaseCommand

from core.models import MutationLog, User
from payroll.models import Payroll
from payroll.creation_progress import (
    cancel_payroll_creation_progress,
    get_payroll_creation_progress,
    persist_creation_progress_to_payroll,
    fail_payroll_creation_progress_by_mutation,
)
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
    _mutation_kind,
    close_completed_creation_mutations,
    close_stale_payroll_mutations_for_payroll,
    _persist_mutation_close,
)

_CREATION_ACTIVE = frozenset({"IN_PROGRESS", "FINALIZING"})


class Command(BaseCommand):
    help = (
        "Remet creation_progress, opensearch_indexing, payment_in_progress, "
        "reconciliation_in_progress et les MutationLog RECEIVED bloqués en état "
        "terminal pour arrêter le polling front."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--payroll-id",
            action="append",
            dest="payroll_ids",
            help="UUID paie (répétable). Sans argument : toutes les paies avec indicateur actif.",
        )
        parser.add_argument(
            "--client-mutation-id",
            action="append",
            dest="client_mutation_ids",
            help="clientMutationId GraphQL (répétable) pour une création paie bloquée.",
        )
        parser.add_argument(
            "--reason",
            default="Ghost polling cleanup.",
            help="Message renvoyé au front dans les progress / mutation log.",
        )
        parser.add_argument(
            "--username",
            default="Admin",
            help="Utilisateur pour save payroll (json_ext).",
        )
        parser.add_argument(
            "--diagnose",
            action="store_true",
            help="Affiche les indicateurs actifs sans les modifier.",
        )

    def handle(self, *args, **options):
        reason = options["reason"]
        username = options["username"]
        diagnose = options["diagnose"]
        user = User.objects.filter(username=username).first()
        if not user:
            user = User.objects.order_by("id").first()
            username = getattr(user, "username", "Admin")

        if not diagnose:
            creation_closed = close_completed_creation_mutations(username=username)
            if creation_closed:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Closed {creation_closed} completed payroll creation mutation log(s)."
                    )
                )

        actions = 0
        client_mutation_ids = options.get("client_mutation_ids") or []
        for cmid in client_mutation_ids:
            actions += self._handle_client_mutation(
                cmid, reason=reason, username=username, diagnose=diagnose
            )

        payroll_ids = options.get("payroll_ids")
        if payroll_ids:
            payrolls = Payroll.objects.filter(id__in=payroll_ids, is_deleted=False)
        else:
            payrolls = Payroll.objects.filter(is_deleted=False)

        for payroll in payrolls:
            actions += self._handle_payroll(
                payroll, user=user, username=username, reason=reason, diagnose=diagnose
            )

        if not payroll_ids and not client_mutation_ids:
            actions += self._handle_orphan_creation_mutations(
                reason=reason, diagnose=diagnose
            )

        if actions == 0:
            self.stdout.write(
                "No ghost polling indicators found "
                "(creation / indexing / payment / reconciliation)."
            )
        elif not diagnose:
            self.stdout.write(self.style.SUCCESS(f"Stopped ghost polling ({actions} action(s))."))

    def _handle_payroll(self, payroll, *, user, username, reason, diagnose):
        payroll_id = str(payroll.id)
        jx = payroll.json_ext or {}
        actions = 0

        creation = get_payroll_creation_progress(payroll_id) or {}
        creation_jx = jx.get("creation_progress") if isinstance(jx.get("creation_progress"), dict) else {}
        needs_creation = (
            creation.get("status") in _CREATION_ACTIVE
            or creation_jx.get("status") in _CREATION_ACTIVE
            or jx.get("keep_taskbar_polling")
            or jx.get("opensearch_indexing_pending")
        )

        from payroll.opensearch_indexing_progress import (
            STATUS_IN_PROGRESS as OS_IN_PROGRESS,
            get_opensearch_indexing_progress_for_payroll,
            fail_payroll_opensearch_indexing,
            is_opensearch_indexing_in_progress,
        )

        os_progress = get_opensearch_indexing_progress_for_payroll(payroll_id) or {}
        os_jx = jx.get("opensearch_indexing_progress") if isinstance(
            jx.get("opensearch_indexing_progress"), dict
        ) else {}
        needs_indexing = (
            is_opensearch_indexing_in_progress(payroll)
            or os_progress.get("status") == OS_IN_PROGRESS
            or os_jx.get("status") == OS_IN_PROGRESS
        )

        pay_flag = is_payment_in_progress(payroll)
        rec_flag = is_reconciliation_in_progress(payroll)
        pay_cache = (get_payroll_payment_progress(payroll_id) or {}).get("status")
        rec_cache = (get_payroll_reconciliation_progress(payroll_id) or {}).get("status")
        pay_jx = (jx.get("payment_progress") or {}).get("status") if isinstance(
            jx.get("payment_progress"), dict
        ) else None
        rec_jx = (jx.get("reconciliation_progress") or {}).get("status") if isinstance(
            jx.get("reconciliation_progress"), dict
        ) else None
        needs_payment = pay_flag or pay_cache == "IN_PROGRESS" or pay_jx == "IN_PROGRESS"
        needs_reconciliation = rec_flag or rec_cache == "IN_PROGRESS" or rec_jx == "IN_PROGRESS"

        if not any((needs_creation, needs_indexing, needs_payment, needs_reconciliation)):
            return 0

        if diagnose:
            self.stdout.write(
                f"[diagnose] {payroll.name} ({payroll_id}): "
                f"creation={needs_creation} indexing={needs_indexing} "
                f"payment={needs_payment} reconciliation={needs_reconciliation}"
            )
            return 1

        if needs_creation:
            progress = cancel_payroll_creation_progress(payroll_id, reason=reason)
            persist_creation_progress_to_payroll(payroll, progress, username)
            payroll.refresh_from_db()
            self.stdout.write(
                self.style.SUCCESS(f"Creation polling stopped: {payroll.name} ({payroll_id})")
            )
            actions += 1

        if needs_indexing:
            fail_payroll_opensearch_indexing(
                payroll_id, reason, username=username
            )
            self.stdout.write(
                self.style.SUCCESS(f"OpenSearch indexing polling stopped: {payroll.name} ({payroll_id})")
            )
            actions += 1

        if needs_payment:
            set_payment_in_progress(payroll, user, False)
            progress = cancel_payroll_payment_progress(payroll_id, reason=reason)
            persist_payment_progress_to_payroll(payroll, progress, username)
            self.stdout.write(
                self.style.SUCCESS(f"Payment polling stopped: {payroll.name} ({payroll_id})")
            )
            actions += 1

        if needs_reconciliation:
            set_reconciliation_in_progress(payroll, user, False)
            progress = cancel_payroll_reconciliation_progress(payroll_id, reason=reason)
            persist_reconciliation_progress_to_payroll(payroll, progress, username)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Reconciliation polling stopped: {payroll.name} ({payroll_id})"
                )
            )
            actions += 1

        closed = close_stale_payroll_mutations_for_payroll(payroll_id, reason=reason)
        if closed:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Closed {closed} stale mutation log(s) for {payroll.name}"
                )
            )
            actions += closed

        return actions

    def _handle_client_mutation(self, client_mutation_id, *, reason, username, diagnose):
        from payroll.creation_progress import get_payroll_creation_progress_by_mutation

        progress = get_payroll_creation_progress_by_mutation(client_mutation_id) or {}
        payroll_id = progress.get("payroll_id")
        mutation_log = (
            MutationLog.objects.filter(
                client_mutation_id=client_mutation_id,
                status=MutationLog.RECEIVED,
            )
            .order_by("-request_date_time")
            .first()
        )

        if diagnose:
            self.stdout.write(
                f"[diagnose] client_mutation_id={client_mutation_id} "
                f"payroll_id={payroll_id} progress={progress.get('status')} "
                f"mutation_received={bool(mutation_log)}"
            )
            return 1 if (progress.get("status") in _CREATION_ACTIVE or mutation_log) else 0

        actions = 0
        if payroll_id:
            payroll = Payroll.objects.filter(id=payroll_id, is_deleted=False).first()
            if payroll:
                from core.models import User

                user = User.objects.filter(username=username).first() or User.objects.order_by("id").first()
                actions += self._handle_payroll(
                    payroll, user=user, username=username, reason=reason, diagnose=False
                )

        fail_payroll_creation_progress_by_mutation(client_mutation_id, reason)
        if mutation_log:
            _persist_mutation_close(mutation_log, as_success=False, message=reason)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Closed creation mutation log for client_mutation_id={client_mutation_id}"
                )
            )
            actions += 1
        elif payroll_id:
            actions += 1
        return actions

    def _handle_orphan_creation_mutations(self, *, reason, diagnose):
        """MutationLog création en RECEIVED sans job réellement actif (spinner fantôme)."""
        from payroll.creation_progress import (
            get_payroll_creation_progress_by_mutation,
            STATUS_COMPLETED,
        )
        from payroll.opensearch_indexing_progress import STATUS_COMPLETED as OS_COMPLETED

        actions = 0
        qs = MutationLog.objects.filter(status=MutationLog.RECEIVED).order_by("-request_date_time")
        for mutation_log in qs[:100]:
            if _mutation_kind(mutation_log) != "creation":
                continue
            cmid = mutation_log.client_mutation_id
            if not cmid:
                continue
            creation = get_payroll_creation_progress_by_mutation(cmid) or {}
            c_status = creation.get("status")
            if c_status == STATUS_COMPLETED:
                continue
            if c_status in _CREATION_ACTIVE:
                payroll_id = creation.get("payroll_id")
                if payroll_id:
                    from payroll.opensearch_indexing_progress import (
                        get_opensearch_indexing_progress_for_payroll,
                    )

                    os_progress = get_opensearch_indexing_progress_for_payroll(str(payroll_id))
                    if (os_progress or {}).get("status") == OS_COMPLETED:
                        continue
                # Création réellement en cours : ne pas couper sans --payroll-id explicite
                if creation.get("processed_beneficiaries", 0) > 0 or creation.get("total_beneficiaries", 0) > 0:
                    continue

            if diagnose:
                self.stdout.write(
                    f"[diagnose] orphan creation mutation {mutation_log.id} cmid={cmid} "
                    f"status={c_status}"
                )
                actions += 1
                continue

            fail_payroll_creation_progress_by_mutation(cmid, reason)
            _persist_mutation_close(mutation_log, as_success=False, message=reason)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Closed orphan creation mutation {mutation_log.id} (cmid={cmid})"
                )
            )
            actions += 1
        return actions
