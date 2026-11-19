"""Notifications email payroll (paiement / réconciliation / callback opérateur)."""

import logging

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from core.models import User
from payroll.apps import PayrollConfig
from payroll.models import BenefitConsumption, BenefitConsumptionStatus, Payroll
from tasks_management.email_delivery import (
    get_user_display_name,
    get_user_email,
    is_task_email_enabled,
    schedule_task_emails,
)
from tasks_management.models import Task

logger = logging.getLogger(__name__)

OPERATOR_CALLBACK_SESSION_KEY = "operator_callback_session"


def _frontend_base_url():
    return (
        getattr(settings, "FRONTEND_URL", None)
        or getattr(settings, "SITE_URL", None)
        or ""
    )


def _resolve_user(user_or_id):
    if user_or_id is None:
        return None
    if isinstance(user_or_id, User):
        return user_or_id
    # Objet user-like (tests / proxies) déjà hydraté
    if not isinstance(user_or_id, (str, int, bytes)) and (
        hasattr(user_or_id, "email")
        or hasattr(user_or_id, "i_user")
        or hasattr(user_or_id, "login_name")
    ):
        return user_or_id
    try:
        return User.objects.filter(id=user_or_id).select_related("i_user").first()
    except Exception:
        return None


def _payroll_validators(payroll):
    """Exécuteurs des tâches maker-checker liées à la paie (acceptation / clôture)."""
    payroll_ct = ContentType.objects.get_for_model(Payroll)
    events = [
        e
        for e in (
            PayrollConfig.payroll_accept_event,
            PayrollConfig.payroll_reconciliation_event,
            PayrollConfig.payroll_reject_event,
        )
        if e
    ]
    tasks = (
        Task.objects.filter(
            entity_type=payroll_ct,
            entity_id=str(payroll.id),
            is_deleted=False,
        )
        .filter(business_event__in=events)
        .select_related("task_group")
        .prefetch_related(
            "task_group__taskexecutor_set__user",
            "task_group__taskexecutor_set__user__i_user",
        )
    )
    users = {}
    for task in tasks:
        if not task.task_group:
            continue
        for executor in task.task_group.taskexecutor_set.filter(is_deleted=False):
            user = executor.user
            if user and user.id not in users:
                users[user.id] = user
    return list(users.values())


def _payroll_stakeholders(payroll, *, extra_users=None):
    """Créateur de la paie + validateurs + utilisateurs supplémentaires (ex. lanceur paiement)."""
    by_id = {}
    creator = getattr(payroll, "user_created", None)
    if creator:
        by_id[creator.id] = creator
    for user in _payroll_validators(payroll):
        by_id[user.id] = user
    for user in extra_users or []:
        if user:
            by_id[user.id] = user
    return list(by_id.values())


def _schedule_to_users(users, subject, body_builder):
    if not is_task_email_enabled():
        return 0
    messages = []
    for user in users:
        email = get_user_email(user)
        if not email:
            logger.warning(
                "Payroll email skipped: no address for user id=%s (%s)",
                getattr(user, "id", None),
                get_user_display_name(user),
            )
            continue
        messages.append(
            {
                "subject": subject,
                "message": body_builder(get_user_display_name(user)),
                "recipient_list": [email],
            }
        )
    if messages:
        schedule_task_emails(messages)
    return len(messages)


def notify_payment_finished(payroll, user, progress=None):
    """Email au lanceur du paiement quand le job passerelle se termine."""
    progress = progress or {}
    status = progress.get("status") or "COMPLETED"
    success = progress.get("success_count", 0)
    rejected = progress.get("rejected_count", 0)
    total = progress.get("total_beneficiaries", 0)
    processed = progress.get("processed_beneficiaries", 0)
    subject = f"[coreMIS] Paiement terminé — {payroll.name} ({status})"
    initiator = _resolve_user(user) or getattr(payroll, "user_updated", None)

    def body(name):
        return (
            f"Bonjour {name},\n\n"
            f"L'envoi des paiements vers la passerelle pour la paie « {payroll.name} » est terminé.\n\n"
            f"Statut : {status}\n"
            f"Traité(s) : {processed} / {total}\n"
            f"Succès : {success}\n"
            f"Rejets : {rejected}\n\n"
            f"Connectez-vous à l'application pour consulter le détail.\n\n"
            f"Cordialement,\n"
            f"L'équipe NAFAMIS/ANIES"
        )

    sent = _schedule_to_users([initiator] if initiator else [], subject, body)
    logger.info(
        "[payroll_notify] payment_finished payroll_id=%s emails=%s status=%s",
        payroll.id,
        sent,
        status,
    )
    return sent


def notify_reconciliation_finished(payroll, user, progress=None):
    """Email au lanceur de la réconciliation manuelle (pull) à la fin du job."""
    progress = progress or {}
    status = progress.get("status") or "COMPLETED"
    success = progress.get("success_count", 0)
    rejected = progress.get("rejected_count", 0)
    total = progress.get("total_beneficiaries", 0)
    processed = progress.get("processed_beneficiaries", 0)
    subject = f"[coreMIS] Réconciliation terminée — {payroll.name} ({status})"
    initiator = _resolve_user(user) or getattr(payroll, "user_updated", None)

    def body(name):
        return (
            f"Bonjour {name},\n\n"
            f"La réconciliation manuelle (passerelle) pour la paie « {payroll.name} » est terminée.\n\n"
            f"Statut : {status}\n"
            f"Traité(s) : {processed} / {total}\n"
            f"Réconcilié(s) : {success}\n"
            f"Non réconcilié(s) : {rejected}\n\n"
            f"Connectez-vous à l'application pour consulter le détail.\n\n"
            f"Cordialement,\n"
            f"L'équipe NAFAMIS/ANIES"
        )

    sent = _schedule_to_users([initiator] if initiator else [], subject, body)
    logger.info(
        "[payroll_notify] reconciliation_finished payroll_id=%s emails=%s status=%s",
        payroll.id,
        sent,
        status,
    )
    return sent


def _count_pending_approve_for_payment(payroll):
    return BenefitConsumption.objects.filter(
        payrollbenefitconsumption__payroll=payroll,
        payrollbenefitconsumption__is_deleted=False,
        is_deleted=False,
        status=BenefitConsumptionStatus.APPROVE_FOR_PAYMENT,
    ).count()


def _get_callback_session(payroll):
    return dict((payroll.json_ext or {}).get(OPERATOR_CALLBACK_SESSION_KEY) or {})


def _save_callback_session(payroll, session, username):
    json_ext = dict(payroll.json_ext or {})
    json_ext[OPERATOR_CALLBACK_SESSION_KEY] = session
    payroll.json_ext = json_ext
    from payroll.opensearch_payroll_status_sync import skip_heavy_opensearch_reindex

    with skip_heavy_opensearch_reindex():
        if payroll.is_dirty():
            payroll.save(username=username or "Admin")


def on_operator_callback_processed(payroll, *, success, already_reconciled=False, username=None):
    """
    Suit la session de callbacks push opérateur :
    - 1er callback → email « début » au créateur + validateurs
    - plus aucune facture APPROVE_FOR_PAYMENT → email « fin »
    """
    if already_reconciled:
        return {"start_emails": 0, "end_emails": 0}

    payroll.refresh_from_db()
    session = _get_callback_session(payroll)
    start_emails = 0
    end_emails = 0
    now = timezone.now().isoformat()

    if not session.get("active") or session.get("end_email_sent"):
        pending = _count_pending_approve_for_payment(payroll)
        # Si success vient d'être enregistré, pending a déjà diminué : +1 pour le snapshot de départ
        pending_at_start = pending + (1 if success else 0)
        session = {
            "active": True,
            "started_at": now,
            "start_email_sent": False,
            "pending_at_start": pending_at_start,
            "callback_success_count": 0,
            "callback_failure_count": 0,
            "finished_at": None,
            "end_email_sent": False,
        }
        start_emails = _notify_operator_callback_started(payroll, session)
        session["start_email_sent"] = True

    if success:
        session["callback_success_count"] = int(session.get("callback_success_count") or 0) + 1
    else:
        session["callback_failure_count"] = int(session.get("callback_failure_count") or 0) + 1

    remaining = _count_pending_approve_for_payment(payroll)
    session["remaining_approve_for_payment"] = remaining
    session["updated_at"] = now

    if remaining == 0 and not session.get("end_email_sent"):
        session["finished_at"] = now
        session["active"] = False
        end_emails = _notify_operator_callback_finished(payroll, session)
        session["end_email_sent"] = True

    _save_callback_session(payroll, session, username)
    return {"start_emails": start_emails, "end_emails": end_emails, "session": session}


def _notify_operator_callback_started(payroll, session):
    subject = f"[coreMIS] Réconciliation opérateur en cours — {payroll.name}"
    pending = session.get("pending_at_start", 0)
    stakeholders = _payroll_stakeholders(payroll)

    def body(name):
        return (
            f"Bonjour {name},\n\n"
            f"L'opérateur a commencé à envoyer des notifications de réconciliation "
            f"(callback push) pour la paie « {payroll.name} ».\n\n"
            f"Factures encore à réconcilier au démarrage : {pending}\n"
            f"Début : {session.get('started_at')}\n\n"
            f"Vous serez informé(e) lorsque toutes les factures en attente auront été traitées.\n\n"
            f"Cordialement,\n"
            f"L'équipe NAFAMIS/ANIES"
        )

    sent = _schedule_to_users(stakeholders, subject, body)
    logger.info(
        "[payroll_notify] operator_callback_started payroll_id=%s emails=%s",
        payroll.id,
        sent,
    )
    return sent


def _notify_operator_callback_finished(payroll, session):
    subject = f"[coreMIS] Réconciliation opérateur terminée — {payroll.name}"
    stakeholders = _payroll_stakeholders(payroll)

    def body(name):
        return (
            f"Bonjour {name},\n\n"
            f"L'opérateur a terminé l'envoi des réconciliations (callback push) "
            f"pour la paie « {payroll.name} ».\n\n"
            f"Succès callback : {session.get('callback_success_count', 0)}\n"
            f"Échecs callback : {session.get('callback_failure_count', 0)}\n"
            f"Factures encore APPROVE_FOR_PAYMENT : {session.get('remaining_approve_for_payment', 0)}\n"
            f"Fin : {session.get('finished_at')}\n\n"
            f"Vous pouvez procéder à la clôture (Accept and Close) si les conditions sont remplies "
            f"(factures réconciliées + rapport PDF).\n\n"
            f"Cordialement,\n"
            f"L'équipe NAFAMIS/ANIES"
        )

    sent = _schedule_to_users(stakeholders, subject, body)
    logger.info(
        "[payroll_notify] operator_callback_finished payroll_id=%s emails=%s",
        payroll.id,
        sent,
    )
    return sent
