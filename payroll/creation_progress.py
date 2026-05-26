"""Suivi de progression création payroll (cache, visible pendant la transaction)."""

from datetime import datetime, timedelta

from django.core.cache import cache
from django.utils import timezone

CACHE_KEY_PREFIX = "payroll_creation_progress:"
MUTATION_KEY_PREFIX = "payroll_creation_mutation:"
MUTATION_PROGRESS_SUFFIX = ":progress"
CACHE_TIMEOUT_SECONDS = 3600
PROGRESS_UPDATE_INTERVAL = 250
STALE_PROGRESS_MINUTES = 180

STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_FINALIZING = "FINALIZING"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"
STATUS_CANCELLED = "CANCELLED"
STATUS_STALE = "STALE"

BENEFICIARIES_PERCENT_CAP = 95
FINALIZING_PERCENT = 98

TERMINAL_STATUSES = frozenset({
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_STALE,
})

ACTIVE_STATUSES = frozenset({
    STATUS_IN_PROGRESS,
    STATUS_FINALIZING,
})


def _cache_key(payroll_id):
    return f"{CACHE_KEY_PREFIX}{payroll_id}"


def _mutation_cache_key(client_mutation_id):
    return f"{MUTATION_KEY_PREFIX}{client_mutation_id}"


def _mutation_progress_cache_key(client_mutation_id):
    return f"{MUTATION_KEY_PREFIX}{client_mutation_id}{MUTATION_PROGRESS_SUFFIX}"


def link_progress_to_mutation(client_mutation_id, payroll_id):
    if client_mutation_id and payroll_id:
        cache.set(_mutation_cache_key(client_mutation_id), str(payroll_id), CACHE_TIMEOUT_SECONDS)


def resolve_payroll_id_from_mutation(client_mutation_id):
    if not client_mutation_id:
        return None
    payroll_id = cache.get(_mutation_cache_key(client_mutation_id))
    if payroll_id:
        return payroll_id
    from core.models import MutationLog
    from payroll.models import PayrollMutation

    mutation_log = (
        MutationLog.objects.filter(client_mutation_id=client_mutation_id)
        .order_by("-request_date_time")
        .first()
    )
    if not mutation_log:
        return None
    payroll_mutation = (
        PayrollMutation.objects.filter(mutation=mutation_log, payroll__is_deleted=False)
        .select_related("payroll")
        .first()
    )
    if payroll_mutation:
        payroll_id = str(payroll_mutation.payroll_id)
        link_progress_to_mutation(client_mutation_id, payroll_id)
        return payroll_id
    return None


def clear_payroll_creation_progress(payroll_id, client_mutation_id=None):
    if payroll_id:
        cache.delete(_cache_key(payroll_id))
    if client_mutation_id:
        cache.delete(_mutation_cache_key(client_mutation_id))
        cache.delete(_mutation_progress_cache_key(client_mutation_id))


def _in_progress_payload(
    *,
    payroll_id=None,
    client_mutation_id=None,
    total_beneficiaries=0,
    processed_beneficiaries=0,
    started_at=None,
):
    total = max(0, int(total_beneficiaries or 0))
    processed = max(0, int(processed_beneficiaries or 0))
    if total > 0:
        percent = min(BENEFICIARIES_PERCENT_CAP, int(processed * 100 / total))
    elif processed > 0:
        percent = BENEFICIARIES_PERCENT_CAP
    else:
        percent = 0
    return {
        "status": STATUS_IN_PROGRESS,
        "phase": "BENEFICIARIES",
        "total_beneficiaries": total,
        "processed_beneficiaries": processed,
        "percent": percent,
        "started_at": started_at or timezone.now().isoformat(),
        "completed_at": None,
        "error": None,
        "message": None,
        "should_stop_polling": False,
        "payroll_id": str(payroll_id) if payroll_id else None,
        "client_mutation_id": str(client_mutation_id) if client_mutation_id else None,
        "mutation_in_progress": True,
    }


def _clear_stale_indexing_flags_if_done(result, payroll_id=None):
    """Si l'indexation OS est terminée en base, ne plus garder le spinner taskbar."""
    if not result or result.get("status") != STATUS_COMPLETED:
        return result
    if not (result.get("opensearch_indexing_pending") or result.get("taskbar_indexing_active")):
        return result
    if not payroll_id:
        payroll_id = result.get("payroll_id")
    if not payroll_id:
        return result
    try:
        from payroll.opensearch_indexing_progress import (
            STATUS_COMPLETED as OS_COMPLETED,
            get_opensearch_indexing_progress_for_payroll,
            payroll_taskbar_completed_message,
        )

        os_progress = get_opensearch_indexing_progress_for_payroll(str(payroll_id))
        if (os_progress or {}).get("status") != OS_COMPLETED:
            return result
        result = dict(result)
        result["opensearch_indexing_pending"] = False
        result["keep_taskbar_polling"] = False
        result["taskbar_indexing_active"] = False
        result["taskbar_message"] = (
            result.get("taskbar_message")
            or (os_progress or {}).get("message")
            or payroll_taskbar_completed_message(str(payroll_id))
        )
    except ImportError:
        pass
    return result


def _enrich_payload_for_client(payload, payroll_id=None):
    if not payload:
        return None
    result = dict(payload)
    status = result.get("status")

    if status in TERMINAL_STATUSES:
        result["should_stop_polling"] = True
        result["mutation_in_progress"] = False
        return _clear_stale_indexing_flags_if_done(result, payroll_id=payroll_id)

    if status in ACTIVE_STATUSES:
        result["should_stop_polling"] = False
        result["error"] = None
        result["mutation_in_progress"] = True
        # Ne pas vérifier Payroll.objects.exists() ici : pendant transaction.atomic()
        # la paie n'est pas visible aux autres connexions → faux CANCELLED.
        if _is_stale_in_progress(result):
            result["status"] = STATUS_STALE
            result["should_stop_polling"] = True
            result["mutation_in_progress"] = False
            result["error"] = result.get("error") or "Progress timed out; stop polling."
        return result

    return result


def _is_stale_in_progress(payload):
    started_at = payload.get("started_at")
    if not started_at:
        return False
    try:
        started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        if started.tzinfo:
            started = started.replace(tzinfo=None)
    except (TypeError, ValueError):
        return False
    return datetime.now() - started > timedelta(minutes=STALE_PROGRESS_MINUTES)


def begin_payroll_creation_progress(client_mutation_id):
    """
    T0 : dès réception de createPayroll, avant bulk insert.
    Le poll par clientMutationId ne doit pas renvoyer CANCELLED tant que la création n'est pas finie.
    """
    if not client_mutation_id:
        return None
    payload = _in_progress_payload(client_mutation_id=client_mutation_id)
    cache.set(
        _mutation_progress_cache_key(client_mutation_id),
        payload,
        CACHE_TIMEOUT_SECONDS,
    )
    return payload


def register_payroll_creation_progress(client_mutation_id, payroll_id, total_beneficiaries=0):
    """Après création de l'enregistrement Payroll, avant le calcul des benefits."""
    if not payroll_id:
        return None
    link_progress_to_mutation(client_mutation_id, payroll_id)
    payload = start_payroll_creation_progress(
        payroll_id,
        total_beneficiaries,
        client_mutation_id=client_mutation_id,
    )
    if client_mutation_id:
        cache.delete(_mutation_progress_cache_key(client_mutation_id))
    return payload


def start_payroll_creation_progress(payroll_id, total_beneficiaries, client_mutation_id=None):
    existing = cache.get(_cache_key(payroll_id)) or {}
    started_at = existing.get("started_at") or timezone.now().isoformat()
    processed = existing.get("processed_beneficiaries", 0)
    if existing.get("status") in TERMINAL_STATUSES:
        return existing

    payload = _in_progress_payload(
        payroll_id=payroll_id,
        client_mutation_id=client_mutation_id,
        total_beneficiaries=total_beneficiaries,
        processed_beneficiaries=processed,
        started_at=started_at,
    )
    cache.set(_cache_key(payroll_id), payload, CACHE_TIMEOUT_SECONDS)
    if client_mutation_id:
        link_progress_to_mutation(client_mutation_id, payroll_id)
    return payload


def update_payroll_creation_progress(payroll_id, processed_beneficiaries, total_beneficiaries):
    if total_beneficiaries <= 0:
        return
    if processed_beneficiaries % PROGRESS_UPDATE_INTERVAL != 0 and processed_beneficiaries != total_beneficiaries:
        return
    existing = cache.get(_cache_key(payroll_id)) or {}
    if existing.get("status") in TERMINAL_STATUSES:
        return
    percent = min(
        BENEFICIARIES_PERCENT_CAP,
        int(processed_beneficiaries * 100 / total_beneficiaries),
    )
    payload = _in_progress_payload(
        payroll_id=payroll_id,
        client_mutation_id=existing.get("client_mutation_id"),
        total_beneficiaries=total_beneficiaries,
        processed_beneficiaries=processed_beneficiaries,
        started_at=existing.get("started_at"),
    )
    payload["percent"] = percent
    cache.set(_cache_key(payroll_id), payload, CACHE_TIMEOUT_SECONDS)


def begin_payroll_creation_finalizing(payroll_id, processed_beneficiaries, total_beneficiaries):
    """Fin du bulk : enregistrement paie, tâche maker-checker et commit transaction."""
    existing = cache.get(_cache_key(payroll_id)) or {}
    total = max(0, int(total_beneficiaries or 0))
    processed = max(0, int(processed_beneficiaries or 0))
    payload = {
        "status": STATUS_FINALIZING,
        "phase": "FINALIZING",
        "total_beneficiaries": total,
        "processed_beneficiaries": processed,
        "percent": FINALIZING_PERCENT,
        "started_at": existing.get("started_at") or timezone.now().isoformat(),
        "completed_at": None,
        "error": None,
        "message": (
            "Finalisation : enregistrement de la paie, création de la tâche de validation "
            "et validation de la transaction."
        ),
        "should_stop_polling": False,
        "payroll_id": str(payroll_id),
        "client_mutation_id": existing.get("client_mutation_id"),
        "mutation_in_progress": True,
    }
    cache.set(_cache_key(payroll_id), payload, CACHE_TIMEOUT_SECONDS)
    return payload


def complete_payroll_creation_progress(payroll_id, processed_beneficiaries=None, total_beneficiaries=None):
    existing = cache.get(_cache_key(payroll_id)) or {}
    total = total_beneficiaries if total_beneficiaries is not None else existing.get("total_beneficiaries", 0)
    processed = (
        processed_beneficiaries
        if processed_beneficiaries is not None
        else existing.get("processed_beneficiaries", 0)
    )
    payload = {
        "status": STATUS_COMPLETED,
        "phase": "COMPLETED",
        "total_beneficiaries": total,
        "processed_beneficiaries": processed,
        "percent": 100,
        "started_at": existing.get("started_at"),
        "completed_at": timezone.now().isoformat(),
        "error": None,
        "message": None,
        "should_stop_polling": True,
        "payroll_id": str(payroll_id),
        "client_mutation_id": existing.get("client_mutation_id"),
        "mutation_in_progress": False,
        # Modale : fermer dès la fin DB (should_stop_polling=True).
        # L'indexation OS est suivie via mutationLogs / payrollMutationPollStatus uniquement.
        "opensearch_indexing_pending": False,
        "keep_taskbar_polling": False,
        "taskbar_indexing_active": False,
        "taskbar_message": None,
    }
    try:
        from payroll.opensearch_payroll_status_sync import _opensearch_enabled

        if _opensearch_enabled():
            payload["opensearch_indexing_pending"] = True
            payload["taskbar_indexing_active"] = True
    except ImportError:
        pass
    cache.set(_cache_key(payroll_id), payload, CACHE_TIMEOUT_SECONDS)
    return payload


def fail_payroll_creation_progress(payroll_id, error_message, client_mutation_id=None):
    existing = cache.get(_cache_key(payroll_id)) or {}
    payload = {
        "status": STATUS_FAILED,
        "total_beneficiaries": existing.get("total_beneficiaries", 0),
        "processed_beneficiaries": existing.get("processed_beneficiaries", 0),
        "percent": existing.get("percent", 0),
        "started_at": existing.get("started_at"),
        "error": str(error_message),
        "completed_at": timezone.now().isoformat(),
        "should_stop_polling": True,
        "payroll_id": str(payroll_id),
        "mutation_in_progress": False,
    }
    cache.set(_cache_key(payroll_id), payload, CACHE_TIMEOUT_SECONDS)
    if client_mutation_id:
        cache.delete(_mutation_progress_cache_key(client_mutation_id))


def fail_payroll_creation_progress_by_mutation(client_mutation_id, error_message):
    if not client_mutation_id:
        return
    payload = {
        "status": STATUS_FAILED,
        "total_beneficiaries": 0,
        "processed_beneficiaries": 0,
        "percent": 0,
        "started_at": timezone.now().isoformat(),
        "error": str(error_message),
        "completed_at": timezone.now().isoformat(),
        "should_stop_polling": True,
        "payroll_id": None,
        "client_mutation_id": str(client_mutation_id),
        "mutation_in_progress": False,
    }
    cache.set(_mutation_progress_cache_key(client_mutation_id), payload, CACHE_TIMEOUT_SECONDS)


def cancel_payroll_creation_progress(payroll_id, reason=None, client_mutation_id=None):
    existing = cache.get(_cache_key(payroll_id)) or {}
    payload = {
        "status": STATUS_CANCELLED,
        "total_beneficiaries": existing.get("total_beneficiaries", 0),
        "processed_beneficiaries": existing.get("processed_beneficiaries", 0),
        "percent": existing.get("percent", 0),
        "started_at": existing.get("started_at"),
        "error": reason or "Creation cancelled.",
        "completed_at": timezone.now().isoformat(),
        "should_stop_polling": True,
        "payroll_id": str(payroll_id),
        "mutation_in_progress": False,
    }
    cache.set(_cache_key(payroll_id), payload, CACHE_TIMEOUT_SECONDS)
    if client_mutation_id:
        cache.delete(_mutation_cache_key(client_mutation_id))
        cache.delete(_mutation_progress_cache_key(client_mutation_id))


def get_payroll_creation_progress(payroll_id):
    payload = cache.get(_cache_key(payroll_id))
    if not payload:
        from payroll.models import Payroll

        payroll = Payroll.objects.filter(id=payroll_id, is_deleted=False).first()
        if payroll:
            payload = (payroll.json_ext or {}).get("creation_progress")
    return _enrich_payload_for_client(payload, payroll_id=payroll_id)


def get_payroll_creation_progress_by_mutation(client_mutation_id):
    if not client_mutation_id:
        return _in_progress_payload()

    payroll_id = resolve_payroll_id_from_mutation(client_mutation_id)
    if payroll_id:
        progress = get_payroll_creation_progress(payroll_id)
        if progress:
            progress["client_mutation_id"] = str(client_mutation_id)
            return progress

    pending = cache.get(_mutation_progress_cache_key(client_mutation_id))
    if pending:
        pending = dict(pending)
        pending["client_mutation_id"] = str(client_mutation_id)
        return _enrich_payload_for_client(pending, payroll_id=pending.get("payroll_id"))

    # Création pas encore enregistrée dans le cache (race très courte) : rester IN_PROGRESS.
    return _in_progress_payload(client_mutation_id=client_mutation_id)


def _creation_progress_snapshot_unchanged(existing, new_payload):
    if existing == new_payload:
        return True
    import json

    return json.dumps(existing or {}, sort_keys=True, default=str) == json.dumps(
        new_payload or {}, sort_keys=True, default=str
    )


def persist_creation_progress_to_payroll(payroll, progress_payload, username):
    if not progress_payload:
        return
    existing = (payroll.json_ext or {}).get("creation_progress")
    if _creation_progress_snapshot_unchanged(existing, progress_payload):
        return
    json_ext = dict(payroll.json_ext or {})
    json_ext["creation_progress"] = progress_payload
    payroll.json_ext = json_ext
    from payroll.opensearch_payroll_status_sync import skip_heavy_opensearch_reindex

    with skip_heavy_opensearch_reindex():
        if payroll.is_dirty():
            payroll.save(username=username)
