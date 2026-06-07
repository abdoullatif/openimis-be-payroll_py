"""Suivi de progression paiement passerelle (tâche Celery, cache + json_ext)."""

from datetime import datetime, timedelta
import logging

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

CACHE_KEY_PREFIX = "payroll_payment_progress:"
# Paiements massifs (>100k) : cache et seuil STALE alignés sur des jobs de plusieurs heures.
CACHE_TIMEOUT_SECONDS = 86400
PROGRESS_UPDATE_INTERVAL = 50
STALE_PAYMENT_PROGRESS_MINUTES = 480
# Worker Celery tué / plus de mises à jour : libérer le verrou plus tôt.
PAYMENT_WORKER_STALE_MINUTES = 3

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
    logger.info(
        "[payment_progress] start payroll_id=%s total=%s username=%s",
        payroll_id,
        total_beneficiaries,
        username,
    )
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
    logger.info(
        "[payment_progress] update payroll_id=%s processed=%s/%s success=%s rejected=%s",
        payroll_id,
        processed,
        total,
        success_count,
        rejected_count,
    )
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
    logger.info(
        "[payment_progress] complete payroll_id=%s processed=%s/%s success=%s rejected=%s",
        payroll_id,
        processed,
        total,
        success_count,
        rejected_count,
    )
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
    logger.warning(
        "[payment_progress] fail payroll_id=%s error=%s",
        payroll_id,
        error_message,
    )
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


def _is_stale_payment_progress(payload, *, minutes=None):
    """Inactivité (dernière mise à jour), pas seulement la durée depuis le démarrage."""
    reference_at = payload.get("updated_at") or payload.get("started_at")
    reference = _parse_progress_timestamp(reference_at)
    if not reference:
        return False
    limit = minutes if minutes is not None else STALE_PAYMENT_PROGRESS_MINUTES
    return datetime.now() - reference > timedelta(minutes=limit)


def _is_payment_recently_updated(payload, minutes=PAYMENT_WORKER_STALE_MINUTES):
    reference_at = payload.get("updated_at") or payload.get("started_at")
    reference = _parse_progress_timestamp(reference_at)
    if not reference:
        return False
    return datetime.now() - reference <= timedelta(minutes=minutes)


def _release_payment_in_progress_lock(payroll):
    from payroll.reconciliation_lock import set_payment_in_progress

    user = getattr(payroll, "user_updated", None) or getattr(payroll, "user_created", None)
    if user:
        set_payment_in_progress(payroll, user, False)
        return

    class _FallbackUser:
        username = "Admin"

    set_payment_in_progress(payroll, _FallbackUser(), False)


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
    username = (
        getattr(getattr(payroll, "user_updated", None), "username", None)
        or getattr(getattr(payroll, "user_created", None), "username", None)
        or "Admin"
    )
    persist_payment_progress_to_payroll(payroll, recovered, username)
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
    logger.warning(
        "[payment_progress] cancel payroll_id=%s reason=%s",
        payroll_id,
        reason,
    )
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
    logger.info(
        "[payment_progress] resolve payroll_id=%s status=%s flag_active=%s updated_at=%s",
        payroll_id,
        status,
        flag_active,
        payload.get("updated_at"),
    )
    if status in TERMINAL_STATUSES:
        if flag_active and status == STATUS_STALE and _is_payment_recently_updated(payload):
            return _recover_active_payment_progress(payroll, payload)
        if flag_active and status in (STATUS_STALE, STATUS_CANCELLED):
            _release_payment_in_progress_lock(payroll)
        result = dict(payload)
        result["should_stop_polling"] = True
        return result

    if status == STATUS_IN_PROGRESS:
        if flag_active and _is_stale_payment_progress(
            payload, minutes=PAYMENT_WORKER_STALE_MINUTES
        ):
            _release_payment_in_progress_lock(payroll)
            return cancel_payroll_payment_progress(
                payroll_id,
                "Payment interrupted. The worker may have been stopped. You can start a new payment.",
            )
        if not flag_active:
            # Progression récente : mutation / Celery pas encore flagués (polling trop tôt).
            if not _is_stale_payment_progress(payload):
                result = dict(payload)
                result["should_stop_polling"] = False
                return result
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
