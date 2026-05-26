"""
Progression indexation OpenSearch après création paie (Celery, barre de tâches).

La modale creation_progress passe à COMPLETED (100 %) dès la fin DB.
Cette progression garde MutationLog en RECEIVED jusqu'à la fin de l'indexation.
"""

import logging
from datetime import datetime, timedelta

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

CACHE_KEY_PREFIX = "payroll_opensearch_indexing:"
MUTATION_KEY_PREFIX = "payroll_opensearch_indexing_mutation:"
CACHE_TIMEOUT_SECONDS = 3600
STALE_INDEXING_MINUTES = 120

STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"
STATUS_STALE = "STALE"

TERMINAL_STATUSES = frozenset({STATUS_COMPLETED, STATUS_FAILED, STATUS_STALE})

INDEXING_MESSAGE = "Indexation OpenSearch en cours."


def format_payroll_datetime_for_taskbar(payroll):
    """Affichage taskbar après indexation : ex. 2026-05-24 18:05."""
    if not payroll:
        return None
    dt = getattr(payroll, "date_created", None)
    if not dt:
        return None
    if timezone.is_aware(dt):
        dt = timezone.localtime(dt)
    return dt.strftime("%Y-%m-%d %H:%M")


def payroll_taskbar_completed_message(payroll_id):
    from payroll.models import Payroll

    payroll = Payroll.objects.filter(id=payroll_id, is_deleted=False).only("date_created").first()
    return format_payroll_datetime_for_taskbar(payroll)


def _cache_key(payroll_id):
    return f"{CACHE_KEY_PREFIX}{payroll_id}"


def _mutation_cache_key(client_mutation_id):
    return f"{MUTATION_KEY_PREFIX}{client_mutation_id}"


def link_indexing_to_mutation(client_mutation_id, payroll_id):
    if client_mutation_id and payroll_id:
        cache.set(_mutation_cache_key(client_mutation_id), str(payroll_id), CACHE_TIMEOUT_SECONDS)


def resolve_payroll_id_from_indexing_mutation(client_mutation_id):
    if not client_mutation_id:
        return None
    return cache.get(_mutation_cache_key(client_mutation_id))


def _in_progress_payload(*, payroll_id, client_mutation_id=None, started_at=None):
    return {
        "status": STATUS_IN_PROGRESS,
        "phase": "OPENSEARCH",
        "percent": 99,
        "started_at": started_at or timezone.now().isoformat(),
        "completed_at": None,
        "error": None,
        "message": INDEXING_MESSAGE,
        "should_stop_polling": False,
        "payroll_id": str(payroll_id),
        "client_mutation_id": str(client_mutation_id) if client_mutation_id else None,
        "mutation_in_progress": True,
    }


def _is_stale(payload):
    started_at = (payload or {}).get("started_at")
    if not started_at:
        return False
    try:
        started = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
        if started.tzinfo:
            started = started.replace(tzinfo=None)
    except (TypeError, ValueError):
        return False
    return datetime.now() - started > timedelta(minutes=STALE_INDEXING_MINUTES)


def is_opensearch_indexing_in_progress(payroll):
    if not payroll:
        return False
    json_ext = payroll.json_ext or {}
    if json_ext.get("opensearch_indexing_in_progress"):
        return True
    payload = json_ext.get("opensearch_indexing_progress") or {}
    return payload.get("status") == STATUS_IN_PROGRESS


def persist_opensearch_indexing_to_payroll(payroll, payload, username):
    if not payroll or not payload:
        return
    json_ext = dict(payroll.json_ext or {})
    json_ext["opensearch_indexing_progress"] = payload
    json_ext["opensearch_indexing_in_progress"] = payload.get("status") == STATUS_IN_PROGRESS
    payroll.json_ext = json_ext
    from payroll.opensearch_payroll_status_sync import skip_heavy_opensearch_reindex

    with skip_heavy_opensearch_reindex():
        if payroll.is_dirty():
            payroll.save(username=username)


def start_payroll_opensearch_indexing(
    payroll_id, client_mutation_id=None, username=None, *, persist_to_db=True
):
    payload = _in_progress_payload(
        payroll_id=payroll_id,
        client_mutation_id=client_mutation_id,
    )
    cache.set(_cache_key(payroll_id), payload, CACHE_TIMEOUT_SECONDS)
    if client_mutation_id:
        link_indexing_to_mutation(client_mutation_id, payroll_id)
    if persist_to_db and username:
        from payroll.models import Payroll

        payroll = Payroll.objects.filter(id=payroll_id, is_deleted=False).first()
        if payroll:
            persist_opensearch_indexing_to_payroll(payroll, payload, username)
    return payload


def complete_payroll_opensearch_indexing(payroll_id, client_mutation_id=None, username=None):
    existing = cache.get(_cache_key(payroll_id)) or {}
    completed_message = payroll_taskbar_completed_message(payroll_id)
    payload = {
        "status": STATUS_COMPLETED,
        "phase": "OPENSEARCH",
        "percent": 100,
        "started_at": existing.get("started_at"),
        "completed_at": timezone.now().isoformat(),
        "error": None,
        "message": completed_message,
        "should_stop_polling": True,
        "payroll_id": str(payroll_id),
        "client_mutation_id": client_mutation_id or existing.get("client_mutation_id"),
        "mutation_in_progress": False,
    }
    cache.set(_cache_key(payroll_id), payload, CACHE_TIMEOUT_SECONDS)
    if client_mutation_id:
        link_indexing_to_mutation(client_mutation_id, payroll_id)
    if username:
        from payroll.models import Payroll

        payroll = Payroll.objects.filter(id=payroll_id, is_deleted=False).first()
        if payroll:
            persist_opensearch_indexing_to_payroll(payroll, payload, username)
    finalize_creation_taskbar_after_opensearch(payroll_id, username=username)
    return payload


def fail_payroll_opensearch_indexing(payroll_id, error_message, client_mutation_id=None, username=None):
    existing = cache.get(_cache_key(payroll_id)) or {}
    payload = {
        "status": STATUS_FAILED,
        "phase": "OPENSEARCH",
        "percent": existing.get("percent", 99),
        "started_at": existing.get("started_at"),
        "completed_at": timezone.now().isoformat(),
        "error": str(error_message),
        "message": str(error_message),
        "should_stop_polling": True,
        "payroll_id": str(payroll_id),
        "client_mutation_id": client_mutation_id or existing.get("client_mutation_id"),
        "mutation_in_progress": False,
    }
    cache.set(_cache_key(payroll_id), payload, CACHE_TIMEOUT_SECONDS)
    if client_mutation_id:
        link_indexing_to_mutation(client_mutation_id, payroll_id)
    if username:
        from payroll.models import Payroll

        payroll = Payroll.objects.filter(id=payroll_id, is_deleted=False).first()
        if payroll:
            persist_opensearch_indexing_to_payroll(payroll, payload, username)
    finalize_creation_taskbar_after_opensearch(payroll_id, username=username)
    return payload


def finalize_creation_taskbar_after_opensearch(payroll_id, username=None):
    """Arrête le polling taskbar côté front (keep_taskbar_polling / opensearch_indexing_pending)."""
    from payroll.creation_progress import (
        CACHE_TIMEOUT_SECONDS as CREATION_CACHE_TIMEOUT,
        STATUS_COMPLETED as CREATION_COMPLETED,
        _cache_key as creation_cache_key,
        persist_creation_progress_to_payroll,
    )

    creation_payload = cache.get(creation_cache_key(payroll_id)) or {}
    if creation_payload.get("status") != CREATION_COMPLETED:
        from payroll.models import Payroll

        payroll = Payroll.objects.filter(id=payroll_id, is_deleted=False).first()
        if payroll:
            creation_payload = (payroll.json_ext or {}).get("creation_progress") or {}
    if creation_payload.get("status") != CREATION_COMPLETED:
        return creation_payload

    creation_payload = dict(creation_payload)
    creation_payload["opensearch_indexing_pending"] = False
    creation_payload["keep_taskbar_polling"] = False
    creation_payload["taskbar_indexing_active"] = False
    creation_payload["taskbar_message"] = payroll_taskbar_completed_message(payroll_id)
    cache.set(creation_cache_key(payroll_id), creation_payload, CREATION_CACHE_TIMEOUT)

    from payroll.models import Payroll

    payroll = Payroll.objects.filter(id=payroll_id, is_deleted=False).first()
    if payroll:
        save_as = username
        if not save_as:
            from core.models import User

            save_as = getattr(User.objects.order_by("id").first(), "username", None) or "System"
        persist_creation_progress_to_payroll(payroll, creation_payload, save_as)
    return creation_payload


def get_payroll_opensearch_indexing_progress(payroll_id):
    return cache.get(_cache_key(payroll_id))


def get_opensearch_indexing_progress_by_mutation(client_mutation_id):
    if not client_mutation_id:
        return None
    payroll_id = resolve_payroll_id_from_indexing_mutation(client_mutation_id)
    if not payroll_id:
        from payroll.creation_progress import resolve_payroll_id_from_mutation

        payroll_id = resolve_payroll_id_from_mutation(client_mutation_id)
    if payroll_id:
        return get_opensearch_indexing_progress_for_payroll(payroll_id)
    return None


def get_opensearch_indexing_progress_for_payroll(payroll_id, *, prefer_db=True):
    """
    État indexation pour la taskbar.

    prefer_db=True : json_ext paie (écrit par Celery) prime sur le cache LocMem du runserver.
    """
    db_progress = None
    if prefer_db:
        from payroll.models import Payroll

        payroll = Payroll.objects.filter(id=payroll_id, is_deleted=False).first()
        if payroll:
            db_progress = (payroll.json_ext or {}).get("opensearch_indexing_progress")

    cache_progress = cache.get(_cache_key(payroll_id))
    if db_progress:
        if cache_progress and cache_progress.get("status") == STATUS_IN_PROGRESS:
            if db_progress.get("status") in TERMINAL_STATUSES:
                return db_progress
        return db_progress
    return cache_progress


def is_opensearch_indexing_expected_for_payroll(payroll):
    from payroll.opensearch_payroll_status_sync import _opensearch_enabled

    if not _opensearch_enabled():
        return False
    if not payroll:
        return False
    progress = get_opensearch_indexing_progress_for_payroll(str(payroll.id))
    if not progress:
        return False
    return progress.get("status") == STATUS_IN_PROGRESS


def begin_payroll_opensearch_indexing_after_db(
    payroll_id, *, client_mutation_id=None, username=None
):
    """
    Démarre le suivi indexation (cache seulement) après creation_progress COMPLETED.

    La persistance json_ext est faite au démarrage de la tâche Celery pour ne pas
    rallonger la réponse HTTP après une grosse paie.
    """
    from payroll.opensearch_payroll_status_sync import _opensearch_enabled

    if not _opensearch_enabled():
        return False

    start_payroll_opensearch_indexing(
        payroll_id,
        client_mutation_id=client_mutation_id,
        username=username,
        persist_to_db=False,
    )
    return True


def reopen_mutation_log_for_opensearch_indexing(client_mutation_id):
    """
    Le core openIMIS passe MutationLog en SUCCESS dès la fin HTTP de createPayroll.
    Remet RECEIVED tant que l'indexation Celery n'est pas terminée (spinner taskbar).
    """
    from core.models import MutationLog
    from payroll.mutation_log_task_bar import _closed_cache_key
    from payroll.opensearch_payroll_status_sync import _opensearch_enabled

    if not client_mutation_id or not _opensearch_enabled():
        return False

    payload = get_opensearch_indexing_progress_by_mutation(client_mutation_id)
    payroll_id = resolve_payroll_id_from_indexing_mutation(client_mutation_id)
    if not payload and payroll_id:
        from payroll.models import Payroll

        payroll = Payroll.objects.filter(id=payroll_id, is_deleted=False).first()
        if payroll:
            payload = (payroll.json_ext or {}).get("opensearch_indexing_progress")

    if not payload or payload.get("status") != STATUS_IN_PROGRESS:
        return False

    mutation_log = (
        MutationLog.objects.filter(client_mutation_id=client_mutation_id)
        .order_by("-request_date_time")
        .first()
    )
    if not mutation_log:
        return False

    if mutation_log.status == MutationLog.SUCCESS:
        MutationLog.objects.filter(pk=mutation_log.pk).update(
            status=MutationLog.RECEIVED,
            error=None,
        )
        cache.delete(_closed_cache_key(mutation_log.id))
        logger.info(
            "MutationLog reopened for OpenSearch indexing client_mutation_id=%s",
            client_mutation_id,
        )
        return True
    return False


def dispatch_payroll_opensearch_indexing_task(
    payroll_id, *, client_mutation_id=None, username=None
):
    from payroll.opensearch_payroll_status_sync import _opensearch_enabled

    if not _opensearch_enabled():
        return False

    from payroll.tasks import sync_payroll_creation_to_opensearch_task

    sync_payroll_creation_to_opensearch_task.delay(
        str(payroll_id),
        client_mutation_id=client_mutation_id,
        username=username,
    )
    return True


def schedule_payroll_opensearch_indexing_after_creation(
    payroll_id, *, client_mutation_id=None, username=None
):
    """Compat : démarre le suivi puis envoie la tâche Celery."""
    if not begin_payroll_opensearch_indexing_after_db(
        payroll_id,
        client_mutation_id=client_mutation_id,
        username=username,
    ):
        return False
    return dispatch_payroll_opensearch_indexing_task(
        payroll_id,
        client_mutation_id=client_mutation_id,
        username=username,
    )


def reconcile_creation_task_bar_progress(mutation_log, client_mutation_id, *, persist):
    """
    Barre de tâches création paie : DB (creation_progress) puis indexation OpenSearch.
    """
    from core.models import MutationLog
    from payroll.creation_progress import get_payroll_creation_progress_by_mutation
    from payroll.mutation_log_task_bar import (
        TASK_BAR_RECEIVED,
        TASK_BAR_SUCCESS,
        _persist_mutation_close,
    )

    creation = get_payroll_creation_progress_by_mutation(client_mutation_id) or {}
    c_status = creation.get("status")

    if c_status in ("FAILED", "CANCELLED", "STALE"):
        return _task_bar_from_indexing_payload(mutation_log, creation, persist=persist)

    if c_status in ("IN_PROGRESS", "FINALIZING"):
        return {
            "task_bar_status": TASK_BAR_RECEIVED,
            "should_stop_polling": False,
            "status": MutationLog.RECEIVED,
            "message": creation.get("message"),
        }

    if c_status == "COMPLETED":
        from payroll.creation_progress import resolve_payroll_id_from_mutation

        payroll_id = (
            creation.get("payroll_id")
            or resolve_payroll_id_from_indexing_mutation(client_mutation_id)
            or resolve_payroll_id_from_mutation(client_mutation_id)
        )
        payroll = None
        if payroll_id:
            from payroll.models import Payroll

            payroll = Payroll.objects.filter(id=payroll_id, is_deleted=False).first()

        os_payload = None
        if payroll_id:
            os_payload = get_opensearch_indexing_progress_for_payroll(payroll_id)
        if not os_payload:
            os_payload = get_opensearch_indexing_progress_by_mutation(client_mutation_id)

        if not os_payload and payroll and is_opensearch_indexing_expected_for_payroll(payroll):
            os_payload = _in_progress_payload(
                payroll_id=payroll_id,
                client_mutation_id=client_mutation_id,
            )

        if not os_payload:
            from payroll.opensearch_payroll_status_sync import _opensearch_enabled

            if _opensearch_enabled():
                return {
                    "task_bar_status": TASK_BAR_RECEIVED,
                    "should_stop_polling": False,
                    "status": MutationLog.RECEIVED,
                    "message": INDEXING_MESSAGE,
                }

            if persist:
                _persist_mutation_close(
                    mutation_log,
                    as_success=True,
                    message="Payroll created.",
                )
            return {
                "task_bar_status": TASK_BAR_SUCCESS,
                "should_stop_polling": True,
                "status": MutationLog.SUCCESS if persist else MutationLog.RECEIVED,
                "message": None,
            }

        if os_payload.get("status") == STATUS_IN_PROGRESS and _is_stale(os_payload):
            os_payload = dict(os_payload)
            os_payload["status"] = STATUS_STALE
            os_payload["error"] = os_payload.get("error") or "OpenSearch indexing timed out."
            os_payload["message"] = os_payload["error"]
            os_payload["should_stop_polling"] = True

        return _task_bar_from_indexing_payload(mutation_log, os_payload, persist=persist)

    return {
        "task_bar_status": TASK_BAR_RECEIVED,
        "should_stop_polling": False,
        "status": MutationLog.RECEIVED,
        "message": creation.get("message"),
    }


def _task_bar_from_indexing_payload(mutation_log, progress, *, persist):
    """Aligné sur mutation_log_task_bar._task_bar_from_progress (import léger)."""
    from payroll.mutation_log_task_bar import (
        TASK_BAR_CANCELLED,
        TASK_BAR_ERROR,
        TASK_BAR_RECEIVED,
        TASK_BAR_STALE,
        TASK_BAR_SUCCESS,
        _persist_mutation_close,
    )

    prog_status = (progress or {}).get("status")
    message = (progress or {}).get("message") or (progress or {}).get("error")

    if prog_status == STATUS_COMPLETED:
        if not message and progress.get("payroll_id"):
            message = payroll_taskbar_completed_message(progress.get("payroll_id"))
        if persist:
            _persist_mutation_close(
                mutation_log,
                as_success=True,
                message=message or "OpenSearch indexing completed.",
            )
        return {
            "task_bar_status": TASK_BAR_SUCCESS,
            "should_stop_polling": True,
            "status": mutation_log.SUCCESS if persist else mutation_log.RECEIVED,
            "message": message,
        }

    if prog_status in (STATUS_FAILED, STATUS_STALE):
        if persist:
            _persist_mutation_close(
                mutation_log,
                as_success=False,
                message=message or f"OpenSearch indexing {prog_status.lower()}.",
            )
        return {
            "task_bar_status": TASK_BAR_STALE if prog_status == STATUS_STALE else TASK_BAR_ERROR,
            "should_stop_polling": True,
            "status": mutation_log.ERROR if persist else mutation_log.RECEIVED,
            "message": message,
        }

    if prog_status == STATUS_IN_PROGRESS:
        return {
            "task_bar_status": TASK_BAR_RECEIVED,
            "should_stop_polling": False,
            "status": mutation_log.RECEIVED,
            "message": message or INDEXING_MESSAGE,
        }

    if persist:
        _persist_mutation_close(
            mutation_log,
            as_success=False,
            message=message or "No OpenSearch indexing in progress.",
        )
    return {
        "task_bar_status": TASK_BAR_CANCELLED,
        "should_stop_polling": True,
        "status": mutation_log.ERROR if persist else mutation_log.RECEIVED,
        "message": message or "No OpenSearch indexing in progress.",
    }
