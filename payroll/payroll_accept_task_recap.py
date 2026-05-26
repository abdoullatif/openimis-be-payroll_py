"""Récapitulatif tâche maker-checker « acceptation de paie » (liste bénéficiaires optimisée)."""

from decimal import Decimal

from django.db.models import Sum

from payroll.models import BenefitConsumption, Payroll
from payroll.payroll_reconciliation_status import (
    MAX_BENEFITS_IN_TASK_RECAP,
    deep_to_json_safe,
    _payroll_benefits_qs,
)


def _normalize_task_root(data):
    if not data:
        return {}
    if isinstance(data.get("incoming_data"), dict):
        return data["incoming_data"]
    return data if isinstance(data, dict) else {}


def _payroll_from_payload(payload):
    payroll_id = payload.get("id") or payload.get("payroll_id")
    if not payroll_id:
        return None
    try:
        return Payroll.objects.filter(id=payroll_id, is_deleted=False).first()
    except (ValueError, TypeError):
        return None


def build_payroll_benefit_detail_rows(payroll, *, limit=None):
    """Lignes facture/bénéficiaire pour l'écran de tâche (requête limitée, champs minimaux)."""
    limit = limit or MAX_BENEFITS_IN_TASK_RECAP
    qs = (
        _payroll_benefits_qs(payroll)
        .select_related("individual")
        .only(
            "id",
            "code",
            "amount",
            "status",
            "json_ext",
            "individual__first_name",
            "individual__last_name",
            "individual__json_ext",
        )
        .order_by("code")[:limit]
    )
    rows = []
    for benefit in qs:
        individual = benefit.individual
        json_ext = benefit.json_ext or {}
        individual_ext = individual.json_ext if individual else {}
        rows.append({
            "code_facture": benefit.code,
            "montant": str(benefit.amount),
            "statut": benefit.status,
            "nom": getattr(individual, "last_name", None) if individual else None,
            "prenom": getattr(individual, "first_name", None) if individual else None,
            "code_menage": json_ext.get("code_menage") or individual_ext.get("code_menage"),
        })
    return rows


def _aggregate_payroll_totals(payroll):
    from django.db.models import Count

    agg = _payroll_benefits_qs(payroll).aggregate(
        nombre=Count("id"),
        montant_total=Sum("amount"),
    )
    return {
        "nombre_beneficiaires": agg["nombre"] or 0,
        "montant_total_paie": str(agg["montant_total"] or Decimal("0")),
    }


def build_payroll_accept_task_payload(payroll_id, obj_data=None):
    """
    Enrichit le payload de tâche à la création (pré-calcul, pas de recalcul à chaque affichage).
    """
    payroll = Payroll.objects.filter(id=payroll_id, is_deleted=False).first()
    if not payroll:
        base = dict(obj_data or {})
        base["id"] = str(payroll_id)
        return deep_to_json_safe(base)

    payload = dict(obj_data or {})
    payload["id"] = str(payroll.id)
    payload.setdefault("name", payroll.name)
    payload.setdefault("status", payroll.status)
    payload.setdefault("payment_method", payroll.payment_method)

    totals = _aggregate_payroll_totals(payroll)
    total_count = totals["nombre_beneficiaires"]
    detail = build_payroll_benefit_detail_rows(payroll)
    payload.update(totals)
    payload["detail_beneficiaires"] = detail
    payload["benefices_truncated"] = total_count > len(detail)
    if payload["benefices_truncated"]:
        payload["note_liste_tronquee"] = (
            f"Liste limitée aux {len(detail)} premières factures "
            f"({total_count} au total). Consultez la fiche paie pour le détail complet."
        )
    payload["recapitulatif_paie"] = format_payroll_accept_recap_text(payload)
    return deep_to_json_safe(payload)


def format_payroll_accept_recap_text(payload):
    lines = [
        f"Paie : {payload.get('name')} ({payload.get('status')})",
        f"Bénéficiaires / factures : {payload.get('nombre_beneficiaires', 0)}",
        f"Montant total : {payload.get('montant_total_paie', '0')}",
    ]
    if payload.get("payment_plan_id"):
        lines.append(f"Plan de paiement : {payload.get('payment_plan_id')}")
    detail = payload.get("detail_beneficiaires") or []
    if detail:
        lines.append("Bénéficiaires concernés (extrait) :")
        for row in detail[:20]:
            name = " ".join(
                part for part in (row.get("prenom"), row.get("nom")) if part
            ) or "-"
            lines.append(
                f"  - {row.get('code_facture')} : {row.get('montant')} — {name}"
            )
        if payload.get("benefices_truncated"):
            lines.append("  … liste tronquée")
    return "\n".join(lines)


def build_payroll_accept_task_display(data):
    """
    Formate businessData pour l'écran de validation (tâche acceptation paie).
    Recharge la liste depuis la DB si absente (anciennes tâches).
    """
    if not data:
        return data

    root = _normalize_task_root(data)
    payroll = _payroll_from_payload(root)
    display = dict(root)

    if payroll and not display.get("detail_beneficiaires"):
        totals = _aggregate_payroll_totals(payroll)
        detail = build_payroll_benefit_detail_rows(payroll)
        display.update(totals)
        display["detail_beneficiaires"] = detail
        display["benefices_truncated"] = (totals["nombre_beneficiaires"] or 0) > len(detail)
        if display["benefices_truncated"]:
            display["note_liste_tronquee"] = (
                f"Liste limitée aux {len(detail)} premières factures "
                f"({totals['nombre_beneficiaires']} au total)."
            )

    if not display.get("recapitulatif_paie"):
        display["recapitulatif_paie"] = format_payroll_accept_recap_text(display)

    if isinstance(data.get("incoming_data"), dict):
        return {"incoming_data": deep_to_json_safe(display)}
    return deep_to_json_safe(display)
