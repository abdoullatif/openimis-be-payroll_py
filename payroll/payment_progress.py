"""Suivi de progression paiement passerelle (tâche Celery, cache + json_ext)."""

from datetime import datetime, timedelta

from django.core.cache import cache
from django.utils import timezone

CACHE_KEY_PREFIX = "payroll_payment_progress:"
# Paiements massifs (>100k) : cache et seuil STALE alignés sur des jobs de plusieurs heures.
CACHE_TIMEOUT_SECONDS = 86400
PROGRESS_UPDATE_INTERVAL = 50
STALE_PAYMENT_PROGRESS_MINUTES = 480

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
        "phase": "GATEWAY_PAYMENT",
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
        "updated_at": timezone.now().isoformat(),
    }


def _snapshot_progress_to_payroll(payroll_id, payload, username):
    """
    Copie la progression dans payroll.json_ext pour que GraphQL la lise
    sans cache partagé (LocMem entre worker Celery et runserver).
    """
    if not username or not payload:
        return
    from payroll.models import Payroll

    payroll = Payroll.objects.filter(id=payroll_id, is_deleted=False).first()
    if payroll:
        persist_payment_progress_to_payroll(payroll, payload, username)


def start_payroll_payment_progress(payroll_id, total_beneficiaries, username=None):
    payload = _in_progress_payload(
        payroll_id=payroll_id,
        total=total_beneficiaries,
        processed=0,
    )
    cache.set(_cache_key(payroll_id), payload, CACHE_TIMEOUT_SECONDS)
    _snapshot_progress_to_payroll(payroll_id, payload, username)
    return payload


def update_payroll_payment_progress(
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
            started_at = (stored.get("payment_progress") or {}).get("started_at")
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


def complete_payroll_payment_progress(
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


def fail_payroll_payment_progress(payroll_id, error_message, username=None):
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


def get_payroll_payment_progress(payroll_id):
    return cache.get(_cache_key(payroll_id))


def clear_payroll_payment_progress_cache(payroll_id):
    cache.delete(_cache_key(payroll_id))


def _parse_progress_timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo:
            parsed = parsed.replace(tzinfo=None)
        return parsed
    except (TypeError, ValueError):
        return None


def _is_stale_payment_progress(payload):
    """Inactivité (dernière mise à jour), pas seulement la durée depuis le démarrage."""
    reference_at = payload.get("updated_at") or payload.get("started_at")
    reference = _parse_progress_timestamp(reference_at)
    if not reference:
        return False
    return datetime.now() - reference > timedelta(minutes=STALE_PAYMENT_PROGRESS_MINUTES)


def _recover_active_payment_progress(payroll, payload):
    """
    Job Celery encore actif (payment_in_progress) : ne jamais laisser STALE côté polling.
    Réécrit cache + json_ext pour éviter les allers-retours front (cache expiré / STALE en DB).
    """
    payroll_id = str(payroll.id)
    recovered = dict(payload)
    recovered["status"] = STATUS_IN_PROGRESS
    recovered["phase"] = payload.get("phase") or "GATEWAY_PAYMENT"
    recovered["should_stop_polling"] = False
    recovered["completed_at"] = None
    recovered["error"] = None
    recovered["message"] = None
    recovered["updated_at"] = timezone.now().isoformat()
    cache.set(_cache_key(payroll_id), recovered, CACHE_TIMEOUT_SECONDS)
    persist_payment_progress_to_payroll(payroll, recovered, "System")
    return recovered


def cancel_payroll_payment_progress(payroll_id, reason=None, username=None):
    """Arrêt explicite ou job Celery tué : le front doit arrêter le polling."""
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
        "error": reason or "Payment cancelled.",
        "message": reason or "Payment cancelled.",
        "should_stop_polling": True,
        "payroll_id": str(payroll_id),
    }
    cache.set(_cache_key(payroll_id), payload, CACHE_TIMEOUT_SECONDS)
    _snapshot_progress_to_payroll(payroll_id, payload, username)
    return payload


def reconcile_payment_progress_for_payroll(payroll):
    """
    Réponse polling cohérente avec payment_in_progress (json_ext).
    Évite le polling fantôme si le job a été annulé côté backend sans finally Celery.
    """
    from payroll.reconciliation_lock import is_payment_in_progress

    payroll_id = str(payroll.id)
    flag_active = is_payment_in_progress(payroll)
    payload = get_payroll_payment_progress(payroll_id) or (payroll.json_ext or {}).get(
        "payment_progress"
    )

    if not payload:
        if flag_active:
            return _in_progress_payload(payroll_id=payroll_id)
        return {
            "status": STATUS_CANCELLED,
            "phase": "IDLE",
            "should_stop_polling": True,
            "payroll_id": payroll_id,
            "message": "No payment in progress.",
        }

    if isinstance(payload, str):
        payload = {}

    status = payload.get("status")
    if status in TERMINAL_STATUSES:
        if status == STATUS_STALE and flag_active:
            return _recover_active_payment_progress(payroll, payload)
        result = dict(payload)
        result["should_stop_polling"] = True
        return result

    if status == STATUS_IN_PROGRESS:
        if not flag_active:
            return cancel_payroll_payment_progress(
                payroll_id, "Payment cancelled or job interrupted."
            )

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


def persist_payment_progress_to_payroll(payroll, progress_payload, username):
    if not progress_payload:
        return
    existing = (payroll.json_ext or {}).get("payment_progress")
    if _progress_snapshot_unchanged(existing, progress_payload):
        return
    json_ext = dict(payroll.json_ext or {})
    json_ext["payment_progress"] = progress_payload
    payroll.json_ext = json_ext
    from payroll.opensearch_payroll_status_sync import skip_heavy_opensearch_reindex

    with skip_heavy_opensearch_reindex():
        payroll.save(username=username)
