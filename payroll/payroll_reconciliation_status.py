"""État réconciliation / clôture payroll pour l'UI."""

import ast
import json
import re
from datetime import date, datetime
from decimal import Decimal

from django.db.models import Count, Max, Q, Sum

from payroll.models import BenefitConsumption, BenefitConsumptionStatus, PaymentReport
from payroll.reconciliation_lock import (
    _username,
    is_payroll_reconciliation_locked,
    is_reconciliation_in_progress,
)

MAX_BENEFITS_IN_TASK_RECAP = 100


def deep_to_json_safe(value):
    """Sérialise récursivement pour stockage JSONField (évite str(dict) sur objets datetime)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [deep_to_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: deep_to_json_safe(item) for key, item in value.items()}
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


CLOSE_BLOCKER_MISSING_PAYMENT_REPORT = "missing_payment_report"
CLOSE_BLOCKER_NO_RECONCILED_BENEFIT = "no_reconciled_benefit"
CLOSE_BLOCKER_RECONCILIATION_LOCKED = "reconciliation_locked"
CLOSE_BLOCKER_RECONCILIATION_IN_PROGRESS = "reconciliation_in_progress"


def _payroll_benefits_qs(payroll):
    return BenefitConsumption.objects.filter(
        payrollbenefitconsumption__payroll=payroll,
        payrollbenefitconsumption__is_deleted=False,
        is_deleted=False,
    )


def count_reconciled_benefits(payroll):
    return _payroll_benefits_qs(payroll).filter(
        status=BenefitConsumptionStatus.RECONCILED,
    ).count()


def has_payment_report(payroll):
    return PaymentReport.objects.filter(payroll=payroll, is_deleted=False).exists()


def get_close_payroll_blockers(payroll):
    blockers = []
    if is_reconciliation_in_progress(payroll):
        blockers.append(CLOSE_BLOCKER_RECONCILIATION_IN_PROGRESS)
    if is_payroll_reconciliation_locked(payroll):
        blockers.append(CLOSE_BLOCKER_RECONCILIATION_LOCKED)
    if not has_payment_report(payroll):
        blockers.append(CLOSE_BLOCKER_MISSING_PAYMENT_REPORT)
    if count_reconciled_benefits(payroll) < 1:
        blockers.append(CLOSE_BLOCKER_NO_RECONCILED_BENEFIT)
    return blockers


def can_close_payroll(payroll):
    return len(get_close_payroll_blockers(payroll)) == 0


def _sum_amount(qs):
    return qs.aggregate(total=Sum("amount"))["total"] or 0


def _sanitize_recap_literal_string(text):
    """Convertit datetime/Decimal dans un repr Python pour ast.literal_eval."""

    def _replace_datetime(match):
        parts = [int(part.strip()) for part in match.group(1).split(",") if part.strip().isdigit()]
        if len(parts) >= 3:
            value = datetime(*parts[:6])
            return repr(value.isoformat())
        return repr(None)

    text = re.sub(r"[\w.]*datetime(?:\.datetime)?\(([^)]+)\)", _replace_datetime, text)
    text = re.sub(r"Decimal\('([^']+)'\)", r"'\1'", text)
    text = re.sub(r'Decimal\("([^"]+)"\)', r'"\1"', text)
    return text


def _parse_legacy_recap_string(text):
    """
    Extrait les champs d'affichage depuis un ancien repr Python (datetime non literal_eval).
    Évite un recalcul DB pour les grosses paies.
    """
    counts_match = re.search(
        r"counts['\"]:\s*\{['\"]total['\"]:\s*(\d+),\s*['\"]reconciled['\"]:\s*(\d+),"
        r"\s*['\"]approve_for_payment['\"]:\s*(\d+),\s*['\"]other['\"]:\s*(\d+)\}",
        text,
    )
    if not counts_match:
        return {}

    amounts_match = re.search(
        r"amounts['\"]:\s*\{['\"]total['\"]:\s*['\"]([^'\"]*)['\"],\s*['\"]reconciled['\"]:\s*['\"]([^'\"]*)['\"],"
        r"\s*['\"]pending['\"]:\s*['\"]([^'\"]*)['\"]\}",
        text,
    )
    payroll_match = re.search(
        r"payroll['\"]:\s*\{['\"]id['\"]:\s*['\"]([^'\"]*)['\"],\s*['\"]name['\"]:\s*['\"]([^'\"]*)['\"],"
        r"\s*['\"]status['\"]:\s*['\"]([^'\"]*)['\"]",
        text,
    )
    last_at_match = re.search(r"'last_completed_at':\s*('[^']*'|None)", text)
    truncated_match = re.search(r"'benefits_truncated':\s*(True|False)", text)

    last_completed_at = None
    if last_at_match and last_at_match.group(1) not in ("None",):
        last_completed_at = last_at_match.group(1).strip("'")

    return {
        "payroll": {
            "id": payroll_match.group(1) if payroll_match else "",
            "name": payroll_match.group(2) if payroll_match else "",
            "status": payroll_match.group(3) if payroll_match else "",
        },
        "counts": {
            "total": int(counts_match.group(1)),
            "reconciled": int(counts_match.group(2)),
            "approve_for_payment": int(counts_match.group(3)),
            "other": int(counts_match.group(4)),
        },
        "amounts": {
            "total": amounts_match.group(1) if amounts_match else "0",
            "reconciled": amounts_match.group(2) if amounts_match else "0",
            "pending": amounts_match.group(3) if amounts_match else "0",
        },
        "last_completed_at": last_completed_at,
        "reconciled_benefits": [],
        "pending_benefits": [],
        "payment_reports": [],
        "benefits_truncated": truncated_match.group(1) == "True" if truncated_match else False,
    }


def _parse_stored_recap(recap):
    """Récap stocké en dict JSON ou ancien format str (repr Python / JSON)."""
    if isinstance(recap, dict):
        return recap
    if not isinstance(recap, str) or not recap.strip():
        return {}
    text = recap.strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        parsed = ast.literal_eval(_sanitize_recap_literal_string(text))
        if isinstance(parsed, dict):
            return parsed
    except (SyntaxError, ValueError):
        pass
    return _parse_legacy_recap_string(text)


def _incoming_has_precached_flat_fields(incoming_data):
    return isinstance(incoming_data, dict) and incoming_data.get("total_factures") is not None


def count_payroll_benefits(payroll):
    """Nombre total de factures / bénéficiaires de la paie (une requête SQL)."""
    return _aggregate_payroll_benefit_stats(_payroll_benefits_qs(payroll))["total"]


def _recap_display_counts(counts):
    """
    Compteurs pour l'écran « résumé réconciliation ».
    nombre_beneficiaires_selectionnes = total paie (pas la taille de l'extrait GraphQL limité à 100).
    """
    total = counts.get("total", 0) or 0
    reconciled = counts.get("reconciled", 0) or 0
    pending = counts.get("approve_for_payment", 0) or 0
    return {
        "nombre_benefices_trouves": total,
        "nombre_beneficiaires_selectionnes": total,
        "nombre_factures_reconciliees": reconciled,
        "nombre_factures_en_attente": pending,
        "extrait_liste_max": MAX_BENEFITS_IN_TASK_RECAP,
        "texte_reconciliees_sur_total": f"{reconciled} sur {total}",
    }


def _aggregate_payroll_benefit_stats(benefits_qs):
    """Comptages et montants en une requête SQL (évite plusieurs .count())."""
    reconciled_status = BenefitConsumptionStatus.RECONCILED
    pending_status = BenefitConsumptionStatus.APPROVE_FOR_PAYMENT
    agg = benefits_qs.aggregate(
        total=Count("id"),
        reconciled=Count("id", filter=Q(status=reconciled_status)),
        approve_for_payment=Count("id", filter=Q(status=pending_status)),
        total_amount=Sum("amount"),
        reconciled_amount=Sum("amount", filter=Q(status=reconciled_status)),
        pending_amount=Sum("amount", filter=Q(status=pending_status)),
        reconciled_max_updated=Max("date_updated", filter=Q(status=reconciled_status)),
    )
    total = agg["total"] or 0
    reconciled = agg["reconciled"] or 0
    pending = agg["approve_for_payment"] or 0
    pending_amount = agg["pending_amount"] or 0
    reconciled_amount = agg["reconciled_amount"] or 0
    return {
        "total": total,
        "reconciled": reconciled,
        "approve_for_payment": pending,
        "other": max(total - reconciled - pending, 0),
        "total_amount": agg["total_amount"] or 0,
        "reconciled_amount": reconciled_amount,
        "pending_amount": pending_amount,
        "gateway_approved": pending + reconciled,
        "gateway_approved_amount": pending_amount + reconciled_amount,
        "reconciled_max_updated": agg["reconciled_max_updated"],
    }


def _ensure_benefit_lists_in_recap(recap, payroll_id=None):
    """Recharge les listes factures depuis la DB si le récap stocké les a perdues (optimisation / repr)."""
    if not recap or not payroll_id:
        return recap
    counts = recap.get("counts") or {}
    needs_reconciled = counts.get("reconciled", 0) > 0 and not recap.get("reconciled_benefits")
    needs_pending = counts.get("approve_for_payment", 0) > 0 and not recap.get("pending_benefits")
    if not needs_reconciled and not needs_pending:
        return recap
    from payroll.models import Payroll

    payroll = Payroll.objects.filter(id=payroll_id, is_deleted=False).first()
    if not payroll:
        return recap
    fresh = build_payroll_reconciliation_recap(payroll)
    if needs_reconciled:
        recap["reconciled_benefits"] = fresh.get("reconciled_benefits") or []
    if needs_pending:
        recap["pending_benefits"] = fresh.get("pending_benefits") or []
    recap["benefits_truncated"] = fresh.get("benefits_truncated", recap.get("benefits_truncated"))
    return recap


def _build_recap_from_flat_incoming(incoming_data):
    """Reconstruit un récap léger depuis incoming_data sans requête DB."""
    recap = _parse_stored_recap(incoming_data.get("reconciliation_recap"))
    payroll_info = recap.get("payroll") or {}
    result = {
        "payroll": {
            "id": str(incoming_data.get("id") or payroll_info.get("id") or ""),
            "name": incoming_data.get("payroll_name") or payroll_info.get("name"),
            "status": incoming_data.get("statut_paie") or payroll_info.get("status"),
            "payment_method": payroll_info.get("payment_method"),
        },
        "counts": {
            "total": incoming_data.get("total_factures", recap.get("counts", {}).get("total", 0)),
            "reconciled": incoming_data.get(
                "factures_reconciliees", recap.get("counts", {}).get("reconciled", 0)
            ),
            "approve_for_payment": incoming_data.get(
                "factures_en_attente", recap.get("counts", {}).get("approve_for_payment", 0)
            ),
            "other": incoming_data.get(
                "autres_statuts", recap.get("counts", {}).get("other", 0)
            ),
        },
        "amounts": {
            "total": str(incoming_data.get("montant_total") or recap.get("amounts", {}).get("total", 0)),
            "reconciled": str(
                incoming_data.get("montant_reconcilie") or recap.get("amounts", {}).get("reconciled", 0)
            ),
            "pending": str(
                incoming_data.get("montant_en_attente") or recap.get("amounts", {}).get("pending", 0)
            ),
        },
        "last_run": recap.get("last_run"),
        "last_completed_at": incoming_data.get("derniere_reconciliation") or recap.get("last_completed_at"),
        "payment_reports": recap.get("payment_reports") or [],
        "reconciled_benefits": recap.get("reconciled_benefits") or [],
        "pending_benefits": recap.get("pending_benefits") or [],
        "benefits_truncated": recap.get("benefits_truncated", False),
    }
    result.update(_recap_display_counts(result["counts"]))
    payroll_id = incoming_data.get("id") or payroll_info.get("id")
    return _ensure_benefit_lists_in_recap(result, payroll_id=payroll_id)


def _benefit_row(benefit):
    json_ext = benefit.json_ext or {}
    return {
        "code": benefit.code,
        "amount": str(benefit.amount),
        "status": benefit.status,
        "receipt": benefit.receipt or json_ext.get("operator_transaction_id"),
        "reconciliation_source": json_ext.get("reconciliation_source"),
    }


def format_reconciliation_date_only(value):
    """Retourne une date au format YYYY-MM-DD (sans heure)."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    if "T" in text:
        return text.split("T", 1)[0]
    return text[:10]


def resolve_last_reconciliation_at(payroll, reconciled_qs=None):
    """
    Date de dernière réconciliation :
    1) payroll.json_ext.reconciliation_last_completed_at (fin job Celery)
    2) date_updated la plus récente des factures RECONCILED
    3) reconciliation_last_attempt_at max dans json_ext des benefits
    """
    payroll_json = payroll.json_ext or {}
    stored = format_reconciliation_date_only(
        payroll_json.get("reconciliation_last_completed_at")
    )
    if stored:
        return stored

    if reconciled_qs is None:
        reconciled_qs = _payroll_benefits_qs(payroll).filter(
            status=BenefitConsumptionStatus.RECONCILED,
        )

    if reconciled_qs is not None:
        max_updated = reconciled_qs.aggregate(latest=Max("date_updated"))["latest"]
        if max_updated:
            return format_reconciliation_date_only(max_updated)

    latest_benefit = _payroll_benefits_qs(payroll).filter(
        status=BenefitConsumptionStatus.RECONCILED,
    ).order_by("-date_updated").values_list("date_updated", flat=True).first()
    if latest_benefit:
        return format_reconciliation_date_only(latest_benefit)
    return None


def build_payroll_reconciliation_recap(payroll):
    """Récapitulatif métier à attacher à la tâche Accept and Close."""
    benefits_qs = _payroll_benefits_qs(payroll)
    stats = _aggregate_payroll_benefit_stats(benefits_qs)
    reconciled_qs = benefits_qs.filter(status=BenefitConsumptionStatus.RECONCILED)
    pending_qs = benefits_qs.filter(status=BenefitConsumptionStatus.APPROVE_FOR_PAYMENT)

    reconciled_benefits = [
        _benefit_row(b)
        for b in reconciled_qs.order_by("code").only(
            "code", "amount", "status", "receipt", "json_ext"
        )[:MAX_BENEFITS_IN_TASK_RECAP]
    ]
    pending_benefits = [
        _benefit_row(b)
        for b in pending_qs.order_by("code").only(
            "code", "amount", "status", "receipt", "json_ext"
        )[:MAX_BENEFITS_IN_TASK_RECAP]
    ]

    payment_reports = [
        {
            "file_name": report.file_name,
            "date_created": report.date_created.isoformat() if report.date_created else None,
        }
        for report in PaymentReport.objects.filter(payroll=payroll, is_deleted=False).order_by(
            "-date_created"
        )[:5]
    ]

    payroll_json = payroll.json_ext or {}
    last_completed_at = format_reconciliation_date_only(
        payroll_json.get("reconciliation_last_completed_at")
    ) or format_reconciliation_date_only(stats["reconciled_max_updated"])

    return {
        "payroll": {
            "id": str(payroll.id),
            "name": payroll.name,
            "status": payroll.status,
            "payment_method": payroll.payment_method,
        },
        "counts": {
            "total": stats["total"],
            "reconciled": stats["reconciled"],
            "approve_for_payment": stats["approve_for_payment"],
            "other": stats["other"],
        },
        "amounts": {
            "total": str(stats["total_amount"]),
            "reconciled": str(stats["reconciled_amount"]),
            "pending": str(stats["pending_amount"]),
        },
        "last_run": payroll_json.get("reconciliation_last_summary"),
        "last_completed_at": last_completed_at,
        "payment_reports": payment_reports,
        "reconciled_benefits": reconciled_benefits,
        "pending_benefits": pending_benefits,
        "benefits_truncated": (
            stats["total"] > MAX_BENEFITS_IN_TASK_RECAP
            or stats["reconciled"] > MAX_BENEFITS_IN_TASK_RECAP
            or stats["approve_for_payment"] > MAX_BENEFITS_IN_TASK_RECAP
        ),
        **_recap_display_counts(
            {
                "total": stats["total"],
                "reconciled": stats["reconciled"],
                "approve_for_payment": stats["approve_for_payment"],
            }
        ),
    }


def format_reconciliation_recap_text(recap):
    """Résumé texte lisible pour l'écran de tâche."""
    payroll_info = recap.get("payroll") or {}
    counts = recap.get("counts") or {}
    amounts = recap.get("amounts") or {}
    lines = [
        f"Paie : {payroll_info.get('name')} ({payroll_info.get('status')})",
        f"Total factures : {counts.get('total', 0)}",
        f"Factures réconciliées : {counts.get('reconciled', 0)}",
        f"Factures en attente : {counts.get('approve_for_payment', 0)}",
        f"Montant total : {amounts.get('total')}",
        f"Montant réconcilié : {amounts.get('reconciled')}",
    ]
    if recap.get("last_completed_at"):
        lines.append(f"Dernière réconciliation : {recap.get('last_completed_at')}")
    reports = recap.get("payment_reports") or []
    if reports:
        lines.append("Rapports de paiement : " + ", ".join(
            r.get("file_name") for r in reports if r.get("file_name")
        ))
    last_run = recap.get("last_run") or {}
    if last_run:
        lines.append(
            f"Dernier run passerelle : {last_run.get('success_count', 0)} succès, "
            f"{last_run.get('rejected_count', 0)} rejet(s)"
        )
    reconciled = recap.get("reconciled_benefits") or []
    if reconciled:
        lines.append("Factures réconciliées (extrait) :")
        for row in reconciled[:15]:
            lines.append(
                f"  - {row.get('code')} : {row.get('amount')} (reçu {row.get('receipt') or '-'})"
            )
        if recap.get("benefits_truncated"):
            lines.append("  … liste tronquée")
    pending = recap.get("pending_benefits") or []
    if pending:
        lines.append("Factures en attente (extrait) :")
        for row in pending[:10]:
            lines.append(f"  - {row.get('code')} : {row.get('amount')}")
    return "\n".join(lines)


def build_payroll_reconciliation_task_incoming_data(payroll):
    """Payload incoming_data pour la tâche (champs plats + récap structuré)."""
    recap = build_payroll_reconciliation_recap(payroll)
    counts = recap["counts"]
    amounts = recap["amounts"]
    return deep_to_json_safe({
        "id": str(payroll.id),
        "payroll_name": payroll.name,
        "statut_paie": recap["payroll"]["status"],
        "total_factures": counts["total"],
        "factures_reconciliees": counts["reconciled"],
        "factures_en_attente": counts["approve_for_payment"],
        "autres_statuts": counts["other"],
        "montant_total": amounts["total"],
        "montant_reconcilie": amounts["reconciled"],
        "montant_en_attente": amounts["pending"],
        "derniere_reconciliation": recap.get("last_completed_at"),
        "rapports_paiement": ", ".join(
            r["file_name"] for r in recap.get("payment_reports") or [] if r.get("file_name")
        ),
        "recapitulatif_reconciliation": format_reconciliation_recap_text(recap),
        "reconciliation_recap": recap,
    })


def resolve_reconciliation_recap_from_task_data(incoming_data):
    """Retourne un dict récap depuis incoming_data (cache, sans recalcul DB si possible)."""
    if not incoming_data:
        return {}

    recap = _parse_stored_recap(incoming_data.get("reconciliation_recap"))
    if recap.get("counts"):
        payroll_id = incoming_data.get("id") or (recap.get("payroll") or {}).get("id")
        return _ensure_benefit_lists_in_recap(recap, payroll_id=payroll_id)

    if _incoming_has_precached_flat_fields(incoming_data):
        return _build_recap_from_flat_incoming(incoming_data)

    payroll_id = incoming_data.get("id")
    if payroll_id:
        from payroll.models import Payroll

        try:
            payroll = Payroll.objects.get(id=payroll_id, is_deleted=False)
            return build_payroll_reconciliation_recap(payroll)
        except Payroll.DoesNotExist:
            pass
    return {}


def _compact_reconciliation_recap_snapshot(recap, last_run):
    """Récap léger pour affichage UI (sans listes de factures)."""
    counts = recap.get("counts") or {}
    return {
        "payroll": recap.get("payroll"),
        "counts": counts,
        "amounts": recap.get("amounts"),
        "last_completed_at": recap.get("last_completed_at"),
        "last_run": last_run,
        "payment_reports": recap.get("payment_reports") or [],
        "reconciled_benefits": [],
        "pending_benefits": [],
        "benefits_truncated": True,
        **_recap_display_counts(counts),
    }


def resolve_payroll_reconciliation_recap(payroll):
    """
    Récap réconciliation pour l'écran « résumé » (évite de charger 15k benefitConsumption).
    Utilise le snapshot json_ext après un run Celery ; sinon agrégat SQL + extrait limité.
    """
    payroll_json = payroll.json_ext or {}
    snapshot = _parse_stored_recap(payroll_json.get("reconciliation_recap_snapshot"))
    if snapshot.get("counts"):
        last_run = payroll_json.get("reconciliation_last_summary")
        if last_run:
            snapshot["last_run"] = last_run
        return snapshot
    return build_payroll_reconciliation_recap(payroll)


def record_reconciliation_run_summary(payroll, user, success_count, rejected_count):
    from django.utils import timezone

    json_ext = dict(payroll.json_ext or {})
    completed_at = format_reconciliation_date_only(timezone.now())
    last_run = {
        "success_count": success_count,
        "rejected_count": rejected_count,
    }
    json_ext["reconciliation_last_completed_at"] = completed_at
    json_ext["reconciliation_last_summary"] = last_run

    recap = build_payroll_reconciliation_recap(payroll)
    recap["last_completed_at"] = completed_at
    recap["last_run"] = last_run
    json_ext["reconciliation_recap_snapshot"] = deep_to_json_safe(
        _compact_reconciliation_recap_snapshot(recap, last_run)
    )

    payroll.json_ext = json_ext
    from payroll.opensearch_payroll_status_sync import payroll_status_only_save

    payroll_status_only_save(payroll, user)
