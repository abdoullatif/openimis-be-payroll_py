"""
Réconciliation des MutationLog RECEIVED pour la barre de tâches verticale.

Les mutations payroll (paiement, réconciliation) répondent vite et le travail long
tourne en Celery : le journal peut rester en RECEIVED alors que le job est fini ou annulé.
"""

import json
import re
from datetime import datetime, timedelta

from django.core.cache import cache

from core.models import MutationLog

# Délai sans progression avant STALE (secours si aucun suivi payroll actif)
STALE_RECEIVED_MINUTES = 120
CACHE_CLOSED_PREFIX = "payroll_mutation_task_bar_closed:"

# Libellés front courants (fr/en)
_PAYMENT_LABEL_RE = re.compile(
    r"effectuer\s+le\s+paiement|make\s*payment|paiement\s+pour\s+la\s+paie",
    re.I,
)
_RECONCILIATION_LABEL_RE = re.compile(
    r"r[eé]conciliation|reconcile",
    re.I,
)
_CREATION_LABEL_RE = re.compile(
    r"cr[eé]er.*paie|create.*payroll",
    re.I,
)
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.I,
)

TASK_BAR_RECEIVED = "RECEIVED"
TASK_BAR_SUCCESS = "SUCCESS"
TASK_BAR_ERROR = "ERROR"
TASK_BAR_CANCELLED = "CANCELLED"
TASK_BAR_STALE = "STALE"


def _parse_json_content(mutation_log):
    raw = mutation_log.json_content
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def extract_payroll_id_from_mutation_log(mutation_log):
    content = _parse_json_content(mutation_log)
    ids = content.get("ids") or []
    if ids:
        return str(ids[0])
    id_ = content.get("id")
    if id_:
        return str(id_)
    label = mutation_log.client_mutation_label or ""
    match = _UUID_RE.search(label)
    return str(match.group(0)) if match else None


def _mutation_kind(mutation_log):
    label = mutation_log.client_mutation_label or ""
    if _PAYMENT_LABEL_RE.search(label):
        return "payment"
    if _RECONCILIATION_LABEL_RE.search(label):
        return "reconciliation"
    if _CREATION_LABEL_RE.search(label):
        return "creation"
    return None


def _is_stale_received(mutation_log):
    if mutation_log.status != MutationLog.RECEIVED:
        return False
    started = mutation_log.request_date_time
    if not started:
        return False
    if hasattr(started, "replace") and getattr(started, "tzinfo", None):
        started = started.replace(tzinfo=None)
    return datetime.now() - started > timedelta(minutes=STALE_RECEIVED_MINUTES)


def _payroll_background_job_still_active(mutation_log):
    """
    Ne pas marquer STALE tant qu'une création / indexation OS / paiement / réconciliation
    est encore suivie (ex. paie 119k ~35 min en RECEIVED).
    """
    kind = _mutation_kind(mutation_log)
    client_mutation_id = mutation_log.client_mutation_id

    if kind == "creation" and client_mutation_id:
        from payroll.creation_progress import (
            get_payroll_creation_progress_by_mutation,
            resolve_payroll_id_from_mutation,
        )

        creation = get_payroll_creation_progress_by_mutation(client_mutation_id) or {}
        c_status = creation.get("status")
        if c_status in ("IN_PROGRESS", "FINALIZING"):
            return True
        if c_status == "COMPLETED":
            from payroll.opensearch_indexing_progress import (
                STATUS_COMPLETED as OS_COMPLETED,
                STATUS_IN_PROGRESS as OS_IN_PROGRESS,
                get_opensearch_indexing_progress_by_mutation,
                get_opensearch_indexing_progress_for_payroll,
            )

            payroll_id = creation.get("payroll_id") or resolve_payroll_id_from_mutation(
                client_mutation_id
            )
            os_prog = get_opensearch_indexing_progress_by_mutation(client_mutation_id)
            if not os_prog and payroll_id:
                os_prog = get_opensearch_indexing_progress_for_payroll(str(payroll_id))
            if os_prog and os_prog.get("status") == OS_COMPLETED:
                return False
            if os_prog and os_prog.get("status") == OS_IN_PROGRESS:
                return True
            if creation.get("opensearch_indexing_pending") or creation.get(
                "taskbar_indexing_active"
            ):
                return True
        return False

    payroll_id = extract_payroll_id_from_mutation_log(mutation_log)
    if not payroll_id:
        return False

    from payroll.models import Payroll

    payroll = Payroll.objects.filter(id=payroll_id, is_deleted=False).first()
    if not payroll:
        return False

    if kind == "payment":
        from payroll.payment_progress import STATUS_IN_PROGRESS as PAYMENT_IN_PROGRESS
        from payroll.payment_progress import get_payroll_payment_progress
        from payroll.reconciliation_lock import is_payment_in_progress

        if is_payment_in_progress(payroll):
            return True
        progress = get_payroll_payment_progress(str(payroll_id)) or (
            (payroll.json_ext or {}).get("payment_progress")
        )
        return (progress or {}).get("status") == PAYMENT_IN_PROGRESS

    if kind == "reconciliation":
        from payroll.reconciliation_progress import STATUS_IN_PROGRESS as RECON_IN_PROGRESS
        from payroll.reconciliation_progress import get_payroll_reconciliation_progress
        from payroll.reconciliation_lock import is_reconciliation_in_progress

        if is_reconciliation_in_progress(payroll):
            return True
        progress = get_payroll_reconciliation_progress(str(payroll_id)) or (
            (payroll.json_ext or {}).get("reconciliation_progress")
        )
        return (progress or {}).get("status") == RECON_IN_PROGRESS

    return False


def _closed_cache_key(mutation_log_id):
    return f"{CACHE_CLOSED_PREFIX}{mutation_log_id}"


def _persist_mutation_close(mutation_log, *, as_success, message):
    """Met à jour le MutationLog une seule fois (cache anti double-write au poll)."""
    key = _closed_cache_key(mutation_log.id)
    if cache.get(key):
        mutation_log.refresh_from_db()
        return
    if mutation_log.status != MutationLog.RECEIVED:
        cache.set(key, True, 3600)
        return
    if as_success:
        mutation_log.mark_as_successful()
    else:
        mutation_log.mark_as_failed(
            json.dumps([{"message": message, "detail": message}])
        )
    cache.set(key, True, 3600)
    mutation_log.refresh_from_db()


def close_completed_creation_mutations(client_mutation_id=None, *, username=None):
    """
    Ferme les MutationLog RECEIVED de création paie quand DB + indexation OS sont terminées.
    """
    from payroll.creation_progress import (
        STATUS_COMPLETED as CREATION_COMPLETED,
        get_payroll_creation_progress_by_mutation,
    )
    from payroll.opensearch_indexing_progress import (
        STATUS_COMPLETED as OS_COMPLETED,
        get_opensearch_indexing_progress_for_payroll,
        payroll_taskbar_completed_message,
    )

    qs = MutationLog.objects.filter(status=MutationLog.RECEIVED).order_by("-request_date_time")
    if client_mutation_id:
        qs = qs.filter(client_mutation_id=client_mutation_id)
    closed = 0
    for mutation_log in qs[:50]:
        if _mutation_kind(mutation_log) != "creation":
            continue
        cmid = mutation_log.client_mutation_id
        if not cmid:
            continue
        creation = get_payroll_creation_progress_by_mutation(cmid) or {}
        if creation.get("status") != CREATION_COMPLETED:
            continue
        payroll_id = creation.get("payroll_id")
        if not payroll_id:
            continue
        os_progress = get_opensearch_indexing_progress_for_payroll(str(payroll_id))
        if (os_progress or {}).get("status") != OS_COMPLETED:
            continue
        message = (
            (os_progress or {}).get("message")
            or payroll_taskbar_completed_message(str(payroll_id))
        )
        _persist_mutation_close(mutation_log, as_success=True, message=message)
        from payroll.opensearch_indexing_progress import finalize_creation_taskbar_after_opensearch

        finalize_creation_taskbar_after_opensearch(payroll_id, username=username)
        closed += 1
    return closed


def close_stale_payroll_mutations_for_payroll(payroll_id, reason=None, username=None):
    """Ferme les MutationLog RECEIVED liés à une paie (annulation admin, commande management)."""
    from payroll.models import Payroll

    payroll_id = str(payroll_id)
    reason = reason or "Operation cancelled or completed outside mutation log."
    closed = 0
    qs = MutationLog.objects.filter(status=MutationLog.RECEIVED).order_by("-request_date_time")
    for mutation_log in qs[:200]:
        if extract_payroll_id_from_mutation_log(mutation_log) != payroll_id:
            continue
        kind = _mutation_kind(mutation_log)
        if kind not in ("payment", "reconciliation", "creation"):
            continue
        _persist_mutation_close(mutation_log, as_success=False, message=reason)
        closed += 1
    return closed


def reconcile_mutation_log_for_task_bar(mutation_log, *, persist=True):
    """
    Statut affiché barre de tâches + arrêt du polling mutationLogs.

    persist=True : met à jour core_Mutation_Log quand le job async est clairement terminé.
    """
    raw_status = mutation_log.status
    if raw_status == MutationLog.SUCCESS:
        return {
            "task_bar_status": TASK_BAR_SUCCESS,
            "should_stop_polling": True,
            "status": raw_status,
            "message": None,
        }
    if raw_status == MutationLog.ERROR:
        return {
            "task_bar_status": TASK_BAR_ERROR,
            "should_stop_polling": True,
            "status": raw_status,
            "message": mutation_log.error,
        }

    if raw_status != MutationLog.RECEIVED:
        return {
            "task_bar_status": TASK_BAR_RECEIVED,
            "should_stop_polling": True,
            "status": raw_status,
            "message": None,
        }

    if _is_stale_received(mutation_log) and not _payroll_background_job_still_active(
        mutation_log
    ):
        if persist:
            _persist_mutation_close(
                mutation_log,
                as_success=False,
                message="Mutation timed out; background job may have ended.",
            )
        return {
            "task_bar_status": TASK_BAR_STALE,
            "should_stop_polling": True,
            "status": MutationLog.ERROR if persist else MutationLog.RECEIVED,
            "message": "Mutation timed out; stop polling.",
        }

    kind = _mutation_kind(mutation_log)
    client_mutation_id = mutation_log.client_mutation_id

    if kind == "creation" and client_mutation_id:
        from payroll.opensearch_indexing_progress import reconcile_creation_task_bar_progress

        return reconcile_creation_task_bar_progress(
            mutation_log,
            client_mutation_id,
            persist=persist,
        )

    payroll_id = extract_payroll_id_from_mutation_log(mutation_log)
    if not payroll_id:
        return {
            "task_bar_status": TASK_BAR_RECEIVED,
            "should_stop_polling": False,
            "status": MutationLog.RECEIVED,
            "message": None,
        }

    from payroll.models import Payroll

    payroll = Payroll.objects.filter(id=payroll_id, is_deleted=False).first()
    if not payroll:
        if persist:
            _persist_mutation_close(
                mutation_log,
                as_success=False,
                message="Payroll not found.",
            )
        return {
            "task_bar_status": TASK_BAR_ERROR,
            "should_stop_polling": True,
            "status": MutationLog.ERROR if persist else MutationLog.RECEIVED,
            "message": "Payroll not found.",
        }

    if kind == "payment":
        from payroll.payment_progress import reconcile_payment_progress_for_payroll
        from payroll.reconciliation_lock import is_payment_in_progress

        if is_payment_in_progress(payroll):
            return {
                "task_bar_status": TASK_BAR_RECEIVED,
                "should_stop_polling": False,
                "status": MutationLog.RECEIVED,
                "message": None,
            }
        progress = reconcile_payment_progress_for_payroll(payroll) or {}
        return _task_bar_from_progress(mutation_log, progress, persist=persist)

    if kind == "reconciliation":
        from payroll.reconciliation_progress import reconcile_reconciliation_progress_for_payroll
        from payroll.reconciliation_lock import is_reconciliation_in_progress

        if is_reconciliation_in_progress(payroll):
            return {
                "task_bar_status": TASK_BAR_RECEIVED,
                "should_stop_polling": False,
                "status": MutationLog.RECEIVED,
                "message": None,
            }
        progress = reconcile_reconciliation_progress_for_payroll(payroll) or {}
        return _task_bar_from_progress(mutation_log, progress, persist=persist)

    return {
        "task_bar_status": TASK_BAR_RECEIVED,
        "should_stop_polling": False,
        "status": MutationLog.RECEIVED,
        "message": None,
    }


def _task_bar_from_progress(mutation_log, progress, *, persist):
    prog_status = (progress or {}).get("status")
    message = (progress or {}).get("message") or (progress or {}).get("error")
    should_stop = (progress or {}).get("should_stop_polling", False)

    if prog_status == "COMPLETED":
        if persist:
            _persist_mutation_close(
                mutation_log,
                as_success=True,
                message="Background job completed.",
            )
        return {
            "task_bar_status": TASK_BAR_SUCCESS,
            "should_stop_polling": True,
            "status": MutationLog.SUCCESS if persist else MutationLog.RECEIVED,
            "message": message,
        }

    if prog_status in ("FAILED", "CANCELLED", "STALE") or (
        prog_status == "CANCELLED" and (progress or {}).get("phase") == "IDLE"
    ):
        if persist:
            _persist_mutation_close(
                mutation_log,
                as_success=False,
                message=message or f"Job {prog_status.lower()}.",
            )
        return {
            "task_bar_status": TASK_BAR_CANCELLED if prog_status == "CANCELLED" else TASK_BAR_ERROR,
            "should_stop_polling": True,
            "status": MutationLog.ERROR if persist else MutationLog.RECEIVED,
            "message": message,
        }

    if should_stop and prog_status != "IN_PROGRESS":
        if persist:
            _persist_mutation_close(
                mutation_log,
                as_success=False,
                message=message or "Job ended.",
            )
        return {
            "task_bar_status": TASK_BAR_CANCELLED,
            "should_stop_polling": True,
            "status": MutationLog.ERROR if persist else MutationLog.RECEIVED,
            "message": message,
        }

    if prog_status in ("IN_PROGRESS", "FINALIZING"):
        return {
            "task_bar_status": TASK_BAR_RECEIVED,
            "should_stop_polling": False,
            "status": MutationLog.RECEIVED,
            "message": message,
        }

    # Idle / pas de progression : job jamais démarré ou déjà nettoyé
    if persist:
        _persist_mutation_close(
            mutation_log,
            as_success=False,
            message=message or "No background job in progress.",
        )
    return {
        "task_bar_status": TASK_BAR_CANCELLED,
        "should_stop_polling": True,
        "status": MutationLog.ERROR if persist else MutationLog.RECEIVED,
        "message": message or "No background job in progress.",
    }
