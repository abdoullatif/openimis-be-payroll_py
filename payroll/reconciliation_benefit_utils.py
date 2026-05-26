"""Utilitaires de mise à jour BenefitConsumption pour la réconciliation."""

from django.utils import timezone


def save_benefit_if_dirty(benefit, username):
    """Save json_ext passerelle sans ré-indexation OpenSearch lourde (comme le paiement)."""
    if benefit.is_dirty(check_relationship=True):
        from payroll.opensearch_payroll_status_sync import benefit_status_only_save

        benefit_status_only_save(benefit, username)


def merge_benefit_json_ext(benefit, updates):
    """
    Fusionne updates dans json_ext et sauvegarde si nécessaire.
    Ajoute reconciliation_last_attempt_at pour garantir un changement à chaque tentative.
    """
    previous = dict(benefit.json_ext or {})
    merged = previous.copy()
    merged.update(updates)
    merged["reconciliation_last_attempt_at"] = timezone.now().isoformat()
    if merged == previous:
        return False
    benefit.json_ext = merged
    return True


def apply_gateway_pull_result(benefit, gateway_result):
    updates = {
        "reconciliation_source": "gateway_pull",
        "output_gateway": bool(gateway_result["success"]),
        "gateway_reconciliation_success": bool(gateway_result["success"]),
    }
    if gateway_result.get("receipt"):
        updates["operator_transaction_id"] = gateway_result["receipt"]
    return merge_benefit_json_ext(benefit, updates)
