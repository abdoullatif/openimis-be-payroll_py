"""
Statut de polling pour mutations payroll (barre de tâches / paiement).

Quand le front poll mutationLogs avec un clientMutationId qui n'existe pas en base,
totalCount reste 0 et le polling ne s'arrête jamais. Cette query renvoie
shouldStopPolling=true dans ce cas (et peut s'appuyer sur paymentProgress si payrollId fourni).
"""

import graphene

from core.models import MutationLog
from payroll.mutation_log_task_bar import (
    TASK_BAR_CANCELLED,
    reconcile_mutation_log_for_task_bar,
)


class PayrollMutationPollStatusGQLType(graphene.ObjectType):
    found = graphene.Boolean(required=True)
    status = graphene.Int(description="Statut core MutationLog (0=RECEIVED, 1=ERROR, 2=SUCCESS).")
    task_bar_status = graphene.String()
    should_stop_polling = graphene.Boolean(required=True)
    task_bar_message = graphene.String()


def _check_payroll_search_perms(user):
    from django.contrib.auth.models import AnonymousUser

    from payroll.apps import PayrollConfig

    if type(user) is AnonymousUser or not user.id or not user.has_perms(
        PayrollConfig.gql_payroll_search_perms
    ):
        from gettext import gettext as _

        raise PermissionError(_("Unauthorized"))


def resolve_payroll_mutation_poll_status(
    info,
    client_mutation_id,
    payroll_id=None,
):
    from payroll.models import Payroll

    _check_payroll_search_perms(info.context.user)

    mutation_log = (
        MutationLog.objects.filter(client_mutation_id=client_mutation_id)
        .order_by("-request_date_time")
        .first()
    )
    if mutation_log:
        reconciled = reconcile_mutation_log_for_task_bar(mutation_log, persist=True)
        return {
            "found": True,
            "status": reconciled["status"],
            "task_bar_status": reconciled["task_bar_status"],
            "should_stop_polling": reconciled["should_stop_polling"],
            "task_bar_message": reconciled["message"],
        }

    if payroll_id:
        payroll = Payroll.objects.filter(id=payroll_id, is_deleted=False).first()
        if payroll:
            from payroll.opensearch_indexing_progress import (
                INDEXING_MESSAGE,
                is_opensearch_indexing_expected_for_payroll,
            )

            from payroll.opensearch_indexing_progress import (
                get_opensearch_indexing_progress_for_payroll,
                payroll_taskbar_completed_message,
            )

            os_progress = get_opensearch_indexing_progress_for_payroll(str(payroll.id))
            if os_progress and os_progress.get("status") == "COMPLETED":
                return {
                    "found": bool(mutation_log),
                    "status": MutationLog.SUCCESS,
                    "task_bar_status": "SUCCESS",
                    "should_stop_polling": True,
                    "task_bar_message": (
                        os_progress.get("message")
                        or payroll_taskbar_completed_message(str(payroll.id))
                    ),
                }
            if is_opensearch_indexing_expected_for_payroll(payroll):
                return {
                    "found": bool(mutation_log),
                    "status": MutationLog.RECEIVED,
                    "task_bar_status": "RECEIVED",
                    "should_stop_polling": False,
                    "task_bar_message": INDEXING_MESSAGE,
                }

            from payroll.payment_progress import reconcile_payment_progress_for_payroll
            from payroll.reconciliation_lock import is_payment_in_progress

            if is_payment_in_progress(payroll):
                return {
                    "found": False,
                    "status": MutationLog.RECEIVED,
                    "task_bar_status": "RECEIVED",
                    "should_stop_polling": False,
                    "task_bar_message": None,
                }
            progress = reconcile_payment_progress_for_payroll(payroll) or {}
            should_stop = progress.get("should_stop_polling", True)
            prog_status = progress.get("status")
            if prog_status == "COMPLETED":
                task_bar = "SUCCESS"
            elif prog_status in ("FAILED", "CANCELLED", "STALE"):
                task_bar = "ERROR"
            elif prog_status == "IN_PROGRESS":
                task_bar = "RECEIVED"
                should_stop = False
            else:
                task_bar = TASK_BAR_CANCELLED
            return {
                "found": False,
                "status": None,
                "task_bar_status": task_bar,
                "should_stop_polling": should_stop,
                "task_bar_message": progress.get("message") or progress.get("error"),
            }

    return {
        "found": False,
        "status": None,
        "task_bar_status": "NOT_FOUND",
        "should_stop_polling": True,
        "task_bar_message": (
            "Aucun MutationLog pour ce clientMutationId ; "
            "arrêter le polling mutationLogs (utiliser payrollPaymentProgress si besoin)."
        ),
    }
