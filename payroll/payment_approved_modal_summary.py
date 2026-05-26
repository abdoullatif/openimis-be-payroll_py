"""Indicateurs modale « Paiements approuvés » (passerelle + réconciliation)."""

from decimal import Decimal

from payroll.benefit_consumption_query import benefit_consumption_status_counts_for_payroll
from payroll.payroll_reconciliation_status import (
    _aggregate_payroll_benefit_stats,
    _payroll_benefits_qs,
    _recap_display_counts,
)


def _amount_str(value):
    if value is None:
        return "0"
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _gateway_counts_from_payment_progress(payroll, stats):
    """
    Acceptés passerelle = APPROVE_FOR_PAYMENT + RECONCILED (toute facture réconciliée
    est passée par la passerelle avant).

    Repli DB pendant le poll pour éviter 0 / N (cache LocMem worker vs API).
    """
    from payroll.payment_progress import reconcile_payment_progress_for_payroll
    from payroll.reconciliation_lock import is_payment_in_progress

    progress = reconcile_payment_progress_for_payroll(payroll) or {}
    db_gateway_approved = int(stats.get("gateway_approved") or 0)
    db_rejected_other = int(stats.get("other") or 0)

    prog_success = int(progress.get("success_count") or 0)
    prog_rejected = int(progress.get("rejected_count") or 0)
    prog_processed = int(progress.get("processed_beneficiaries") or 0)
    prog_total = int(progress.get("total_beneficiaries") or 0)

    in_progress = is_payment_in_progress(payroll) or progress.get("status") == "IN_PROGRESS"

    if in_progress:
        approved_count = max(prog_success, db_gateway_approved)
    else:
        approved_count = db_gateway_approved

    if in_progress and prog_rejected == 0 and db_rejected_other:
        rejected_count = db_rejected_other
    else:
        rejected_count = max(prog_rejected, 0)

    return {
        "gateway_approved_count": approved_count,
        "gateway_rejected_count": rejected_count,
        "gateway_processed_count": max(prog_processed, approved_count + rejected_count),
        "gateway_total_submitted": max(prog_total, int(stats.get("total") or 0)),
        "payment_job_status": progress.get("status"),
        "payment_job_percent": progress.get("percent"),
        "payment_job_in_progress": in_progress,
    }


def build_payment_approved_modal_summary(payroll):
    """
    Résumé léger pour la modale « Paiements approuvés ».

    Passerelle : effectifs APPROVE_FOR_PAYMENT + RECONCILED (sans montant passerelle).
    """
    stats = _aggregate_payroll_benefit_stats(_payroll_benefits_qs(payroll))
    gateway = _gateway_counts_from_payment_progress(payroll, stats)

    total_invoices = int(stats["total"] or 0)
    reconciled_count = int(stats["reconciled"] or 0)
    approve_for_payment_count = int(stats["approve_for_payment"] or 0)

    if total_invoices > 0:
        reconciled_percent = round(reconciled_count * 100.0 / total_invoices, 1)
    else:
        reconciled_percent = 0.0

    status_counts = benefit_consumption_status_counts_for_payroll(payroll.id)
    db_approve_for_payment = int(status_counts.get("APPROVE_FOR_PAYMENT") or 0)

    invoice_total_amount = _amount_str(stats["total_amount"])
    reconciled_total_amount = _amount_str(stats["reconciled_amount"])
    approve_for_payment_amount = _amount_str(stats["pending_amount"])

    display = _recap_display_counts({
        "total": total_invoices,
        "reconciled": reconciled_count,
        "approve_for_payment": db_approve_for_payment,
    })

    return {
        **gateway,
        "reconciled_count": reconciled_count,
        "total_invoices": total_invoices,
        "reconciled_percent": reconciled_percent,
        "db_approve_for_payment_count": db_approve_for_payment,
        "approve_for_payment_count": approve_for_payment_count,
        "invoice_total_amount": invoice_total_amount,
        "montant_total_factures": invoice_total_amount,
        "reconciled_total_amount": reconciled_total_amount,
        "montant_livre_reconciliation": reconciled_total_amount,
        "approve_for_payment_amount": approve_for_payment_amount,
        "amounts": {
            "total": invoice_total_amount,
            "reconciled": reconciled_total_amount,
            "approve_for_payment": approve_for_payment_amount,
        },
        "counts": {
            "total": total_invoices,
            "reconciled": reconciled_count,
            "approve_for_payment": approve_for_payment_count,
            "gateway_approved": gateway["gateway_approved_count"],
        },
        "texte_paiements_passerelle": (
            f"{gateway['gateway_approved_count']:,} accepté(s) par la passerelle"
            + (
                f", {gateway['gateway_rejected_count']:,} rejeté(s)"
                if gateway["gateway_rejected_count"]
                else ""
            )
        ).replace(",", " "),
        "texte_factures_reconciliees": (
            f"{display.get('texte_reconciliees_sur_total', '')} ({reconciled_percent} %)"
            + f" — montant réconcilié : {reconciled_total_amount}"
            if total_invoices
            else "0 %"
        ),
        "texte_montant_total_factures": (
            f"{total_invoices:,} facture(s) — montant total : {invoice_total_amount}"
        ).replace(",", " "),
    }
