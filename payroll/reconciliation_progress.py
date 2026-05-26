"""Suivi de progression réconciliation passerelle (tâche Celery, cache + json_ext)."""

from datetime import datetime, timedelta

from django.core.cache import cache
from django.utils import timezone

CACHE_KEY_PREFIX = "payroll_reconciliation_progress:"
CACHE_TIMEOUT_SECONDS = 3600
PROGRESS_UPDATE_INTERVAL = 50
STALE_RECONCILIATION_PROGRESS_MINUTES = 120

STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"
STATUS_CANCELLED = "CANCELLED"
STATUS_STALE = "STALE"

TERMINAL_STATUSES = frozenset({
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_STALE,
})


def _cache_key(payroll_id):
    return f"{CACHE_KEY_PREFIX}{payroll_id}"


def _in_progress_payload(
    *,
    payroll_id,
    total=0,
    processed=0,
    success_count=0,
    rejected_count=0,
    started_at=None,
):
    total = max(0, int(total or 0))
    processed = max(0, int(processed or 0))
    if total > 0:
        percent = min(99, int(processed * 100 / total))
    elif processed > 0:
        percent = 99
    else:
        percent = 0
    return {
        "status": STATUS_IN_PROGRESS,
        "phase": "GATEWAY_RECONCILIATION",
        "total_beneficiaries": total,
        "processed_beneficiaries": processed,
        "success_count": success_count,
        "rejected_count": rejected_count,
        "percent": percent,
        "started_at": started_at or timezone.now().isoformat(),
        "completed_at": None,
        "error": None,
        "message": None,
        "should_stop_polling": False,
        "payroll_id": str(payroll_id),
    }


def _snapshot_progress_to_payroll(payroll_id, payload, username):
    if not username or not payload:
        return
    from payroll.models import Payroll

    payroll = Payroll.objects.filter(id=payroll_id, is_deleted=False).first()
    if payroll:
        persist_reconciliation_progress_to_payroll(payroll, payload, username)


def start_payroll_reconciliation_progress(payroll_id, total_beneficiaries, username=None):
    payload = _in_progress_payload(
        payroll_id=payroll_id,
        total=total_beneficiaries,
        processed=0,
    )
    cache.set(_cache_key(payroll_id), payload, CACHE_TIMEOUT_SECONDS)
    _snapshot_progress_to_payroll(payroll_id, payload, username)
    return payload


def update_payroll_reconciliation_progress(
    payroll_id,
    processed,
    total,
    success_count=0,
    rejected_count=0,
    username=None,
):
    total = max(0, int(total or 0))
    processed = max(0, int(processed or 0))
    if total > 0 and processed % PROGRESS_UPDATE_INTERVAL != 0 and processed != total:
        return None
    existing = cache.get(_cache_key(payroll_id)) or {}
    if existing.get("status") in TERMINAL_STATUSES:
        return None
    started_at = existing.get("started_at")
    if not started_at:
        from payroll.models import Payroll

        stored = (
            Payroll.objects.filter(id=payroll_id, is_deleted=False)
            .values_list("json_ext", flat=True)
            .first()
        ) or {}
        if isinstance(stored, dict):
            started_at = (stored.get("reconciliation_progress") or {}).get("started_at")
    payload = _in_progress_payload(
        payroll_id=payroll_id,
        total=total,
        processed=processed,
        success_count=success_count,
        rejected_count=rejected_count,
        started_at=started_at,
    )
    cache.set(_cache_key(payroll_id), payload, CACHE_TIMEOUT_SECONDS)
    _snapshot_progress_to_payroll(payroll_id, payload, username)
    return payload


def complete_payroll_reconciliation_progress(
    payroll_id,
    processed,
    total,
    success_count=0,
    rejected_count=0,
    username=None,
):
    existing = cache.get(_cache_key(payroll_id)) or {}
    payload = {
        "status": STATUS_COMPLETED,
        "phase": "COMPLETED",
        "total_beneficiaries": total,
        "processed_beneficiaries": processed,
        "success_count": success_count,
        "rejected_count": rejected_count,
        "percent": 100,
        "started_at": existing.get("started_at"),
        "completed_at": timezone.now().isoformat(),
        "error": None,
        "message": None,
        "should_stop_polling": True,
        "payroll_id": str(payroll_id),
    }
    cache.set(_cache_key(payroll_id), payload, CACHE_TIMEOUT_SECONDS)
    _snapshot_progress_to_payroll(payroll_id, payload, username)
    return payload


def fail_payroll_reconciliation_progress(payroll_id, error_message, username=None):
    existing = cache.get(_cache_key(payroll_id)) or {}
    payload = {
        "status": STATUS_FAILED,
        "phase": "FAILED",
        "total_beneficiaries": existing.get("total_beneficiaries", 0),
        "processed_beneficiaries": existing.get("processed_beneficiaries", 0),
        "success_count": existing.get("success_count", 0),
        "rejected_count": existing.get("rejected_count", 0),
        "percent": existing.get("percent", 0),
        "started_at": existing.get("started_at"),
        "completed_at": timezone.now().isoformat(),
        "error": str(error_message),
        "message": str(error_message),
        "should_stop_polling": True,
        "payroll_id": str(payroll_id),
    }
    cache.set(_cache_key(payroll_id), payload, CACHE_TIMEOUT_SECONDS)
    _snapshot_progress_to_payroll(payroll_id, payload, username)
    return payload


def get_payroll_reconciliation_progress(payroll_id):
    return cache.get(_cache_key(payroll_id))


def clear_payroll_reconciliation_progress_cache(payroll_id):
    cache.delete(_cache_key(payroll_id))


def _is_stale_reconciliation_progress(payload):
    started_at = payload.get("started_at")
    if not started_at:
        return False
    try:
        started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        if started.tzinfo:
            started = started.replace(tzinfo=None)
    except (TypeError, ValueError):
        return False
    return datetime.now() - started > timedelta(minutes=STALE_RECONCILIATION_PROGRESS_MINUTES)


def cancel_payroll_reconciliation_progress(payroll_id, reason=None, username=None):
    existing = cache.get(_cache_key(payroll_id)) or {}
    payload = {
        "status": STATUS_CANCELLED,
        "phase": "CANCELLED",
        "total_beneficiaries": existing.get("total_beneficiaries", 0),
        "processed_beneficiaries": existing.get("processed_beneficiaries", 0),
        "success_count": existing.get("success_count", 0),
        "rejected_count": existing.get("rejected_count", 0),
        "percent": existing.get("percent", 0),
        "started_at": existing.get("started_at"),
        "completed_at": timezone.now().isoformat(),
        "error": reason or "Reconciliation cancelled.",
        "message": reason or "Reconciliation cancelled.",
        "should_stop_polling": True,
        "payroll_id": str(payroll_id),
    }
    cache.set(_cache_key(payroll_id), payload, CACHE_TIMEOUT_SECONDS)
    _snapshot_progress_to_payroll(payroll_id, payload, username)
    return payload


def reconcile_reconciliation_progress_for_payroll(payroll):
    """
    Réponse polling cohérente avec reconciliation_in_progress (json_ext).
    Évite le polling fantôme si le job a été annulé sans finally Celery.
    """
    from payroll.reconciliation_lock import is_reconciliation_in_progress

    payroll_id = str(payroll.id)
    flag_active = is_reconciliation_in_progress(payroll)
    payload = get_payroll_reconciliation_progress(payroll_id) or (payroll.json_ext or {}).get(
        "reconciliation_progress"
    )

    if not payload:
        if flag_active:
            return _in_progress_payload(payroll_id=payroll_id)
        return {
            "status": STATUS_CANCELLED,
            "phase": "IDLE",
            "should_stop_polling": True,
            "payroll_id": payroll_id,
            "message": "No reconciliation in progress.",
        }

    if isinstance(payload, str):
        payload = {}

    status = payload.get("status")
    if status in TERMINAL_STATUSES:
        result = dict(payload)
        result["should_stop_polling"] = True
        return result

    if status == STATUS_IN_PROGRESS:
        if not flag_active:
            return cancel_payroll_reconciliation_progress(
                payroll_id, "Reconciliation cancelled or job interrupted."
            )
        if _is_stale_reconciliation_progress(payload):
            stale = {
                **payload,
                "status": STATUS_STALE,
                "phase": "STALE",
                "should_stop_polling": True,
                "completed_at": timezone.now().isoformat(),
                "error": "Reconciliation progress timed out; stop polling.",
                "message": "Reconciliation progress timed out; stop polling.",
            }
            cache.set(_cache_key(payroll_id), stale, CACHE_TIMEOUT_SECONDS)
            return stale

    result = dict(payload)
    result["should_stop_polling"] = False
    return result


def _progress_snapshot_unchanged(existing, new_payload):
    if existing == new_payload:
        return True
    import json

    return json.dumps(existing or {}, sort_keys=True, default=str) == json.dumps(
        new_payload or {}, sort_keys=True, default=str
    )


def persist_reconciliation_progress_to_payroll(payroll, progress_payload, username):
    if not progress_payload:
        return
    existing = (payroll.json_ext or {}).get("reconciliation_progress")
    if _progress_snapshot_unchanged(existing, progress_payload):
        return
    json_ext = dict(payroll.json_ext or {})
    json_ext["reconciliation_progress"] = progress_payload
    payroll.json_ext = json_ext
    from payroll.opensearch_payroll_status_sync import skip_heavy_opensearch_reindex

    with skip_heavy_opensearch_reindex():
        payroll.save(username=username)
