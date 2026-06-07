"""Verrouillage et indicateurs d'avancement réconciliation / paiement."""

from django.contrib.contenttypes.models import ContentType
import logging

from payroll.apps import PayrollConfig
from payroll.models import Payroll
from tasks_management.models import Task

logger = logging.getLogger(__name__)

RECONCILIATION_IN_PROGRESS_KEY = "reconciliation_in_progress"
PAYMENT_IN_PROGRESS_KEY = "payment_in_progress"

# Tant qu'une tâche de clôture existe et n'est pas rejetée, plus de réconciliation.
_RECONCILIATION_LOCK_STATUSES = (
    Task.Status.RECEIVED,
    Task.Status.ACCEPTED,
    Task.Status.COMPLETED,
)


def _username(user):
    return getattr(user, "login_name", None) or getattr(user, "username", None) or "System"


def is_payroll_reconciliation_locked(payroll):
    payroll_ct = ContentType.objects.get_for_model(Payroll)
    return Task.objects.filter(
        entity_type=payroll_ct,
        entity_id=str(payroll.id),
        business_event=PayrollConfig.payroll_reconciliation_event,
        status__in=_RECONCILIATION_LOCK_STATUSES,
        is_deleted=False,
    ).exists()


def ensure_payroll_reconciliation_not_locked(payroll):
    if is_payroll_reconciliation_locked(payroll):
        raise ValueError(
            "Reconciliation is locked: Accept and Close has already been triggered for this payroll"
        )


def is_reconciliation_in_progress(payroll):
    return bool((payroll.json_ext or {}).get(RECONCILIATION_IN_PROGRESS_KEY))


def is_payment_in_progress(payroll):
    return bool((payroll.json_ext or {}).get(PAYMENT_IN_PROGRESS_KEY))


def _save_payroll_json_ext_flags(payroll, user):
    """Mise à jour json_ext seule, sans ré-indexation OpenSearch en cascade."""
    if not payroll.is_dirty():
        return
    from payroll.opensearch_payroll_status_sync import skip_heavy_opensearch_reindex

    with skip_heavy_opensearch_reindex():
        payroll.save(username=_username(user))


def set_reconciliation_in_progress(payroll, user, in_progress):
    json_ext = dict(payroll.json_ext or {})
    already = bool(json_ext.get(RECONCILIATION_IN_PROGRESS_KEY))
    if in_progress:
        if already:
            return
        json_ext[RECONCILIATION_IN_PROGRESS_KEY] = True
    else:
        if not already:
            return
        json_ext.pop(RECONCILIATION_IN_PROGRESS_KEY, None)
    payroll.json_ext = json_ext
    _save_payroll_json_ext_flags(payroll, user)


def set_payment_in_progress(payroll, user, in_progress):
    json_ext = dict(payroll.json_ext or {})
    already = bool(json_ext.get(PAYMENT_IN_PROGRESS_KEY))
    if in_progress:
        if already:
            return
        json_ext[PAYMENT_IN_PROGRESS_KEY] = True
    else:
        if not already:
            return
        json_ext.pop(PAYMENT_IN_PROGRESS_KEY, None)
    payroll.json_ext = json_ext
    _save_payroll_json_ext_flags(payroll, user)
    logger.info(
        "[reconciliation_lock] set_payment_in_progress payroll_id=%s in_progress=%s by=%s",
        payroll.id,
        in_progress,
        _username(user),
    )
