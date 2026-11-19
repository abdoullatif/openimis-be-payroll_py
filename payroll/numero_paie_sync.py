"""
Synchronisation du numéro de paiement (numero_paie) depuis l'opérateur
lors des réconciliations (CSV, pull passerelle, callback push).

Règles :
- champ absent / vide → rien
- identique à la valeur stockée → rien
- différent → maj Individual + Beneficiary, historique conservé
"""

from __future__ import annotations

import logging
from typing import Optional

from django.utils import timezone

logger = logging.getLogger(__name__)

NUMERO_PAIE_KEY = "numero_paie"
HISTORY_KEY = "numero_paie_history"
MAX_HISTORY_ENTRIES = 50

# Clés acceptées côté opérateur / CSV
_INCOMING_KEYS = (
    "numeroPaie",
    "numero_paie",
    "NumeroPaie",
    "NUMERO_PAIE",
)

SOURCE_CSV = "csv_reconciliation"
SOURCE_GATEWAY_PULL = "gateway_pull"
SOURCE_OPERATOR_CALLBACK = "operator_callback"

ACTION_SKIPPED_EMPTY = "skipped_empty"
ACTION_UNCHANGED = "unchanged"
ACTION_UPDATED = "updated"
ACTION_SKIPPED_NO_INDIVIDUAL = "skipped_no_individual"


def extract_numero_paie(data) -> Optional[str]:
    """Extrait et normalise numeroPaie / numero_paie depuis un dict, une Series ou une valeur."""
    if data is None:
        return None
    if isinstance(data, (str, int, float)):
        return _normalize(data)
    if isinstance(data, dict):
        for key in _INCOMING_KEYS:
            if key in data and data[key] is not None:
                return _normalize(data[key])
        return None
    # pandas Series / mapping-like
    try:
        for key in _INCOMING_KEYS:
            if hasattr(data, "__contains__") and key in data:
                value = data[key]
                if value is not None and not (isinstance(value, float) and value != value):
                    normalized = _normalize(value)
                    if normalized:
                        return normalized
    except Exception:
        pass
    return None


def _normalize(value) -> Optional[str]:
    if value is None:
        return None
    try:
        import math
        if isinstance(value, float) and math.isnan(value):
            return None
    except Exception:
        pass
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none", "null"):
        return None
    # CSV / Excel peut envoyer 12345.0
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return text


def get_stored_numero_paie(individual) -> Optional[str]:
    if individual is None:
        return None
    return _normalize((individual.json_ext or {}).get(NUMERO_PAIE_KEY))


def sync_numero_paie_from_operator(
    benefit,
    incoming_numero_paie,
    *,
    source,
    username,
    invoice_id=None,
):
    """
    Compare / met à jour numero_paie sur Individual (+ Beneficiaries liés).

    Retourne un dict : {action, previous, new, source, ...}
    """
    new_value = _normalize(incoming_numero_paie)
    if not new_value:
        return {
            "action": ACTION_SKIPPED_EMPTY,
            "previous": None,
            "new": None,
            "source": source,
        }

    individual = getattr(benefit, "individual", None)
    if individual is None and getattr(benefit, "individual_id", None):
        from individual.models import Individual

        individual = Individual.objects.filter(
            id=benefit.individual_id, is_deleted=False
        ).first()

    if individual is None:
        logger.warning(
            "[numero_paie_sync] no individual for benefit=%s source=%s",
            getattr(benefit, "code", None),
            source,
        )
        return {
            "action": ACTION_SKIPPED_NO_INDIVIDUAL,
            "previous": None,
            "new": new_value,
            "source": source,
        }

    previous = get_stored_numero_paie(individual)
    if previous == new_value:
        return {
            "action": ACTION_UNCHANGED,
            "previous": previous,
            "new": new_value,
            "source": source,
        }

    now = timezone.now().isoformat()
    history_entry = {
        "previous": previous,
        "new": new_value,
        "source": source,
        "at": now,
        "invoiceId": invoice_id or getattr(benefit, "code", None),
        "username": username,
    }

    _apply_numero_paie_on_json_ext(individual, new_value, history_entry)
    _save_history_model(individual, username)

    updated_beneficiaries = 0
    try:
        from social_protection.models import Beneficiary

        beneficiaries = Beneficiary.objects.filter(
            individual_id=individual.id,
            is_deleted=False,
        )
        for beneficiary in beneficiaries:
            _apply_numero_paie_on_json_ext(beneficiary, new_value, history_entry)
            _save_history_model(beneficiary, username)
            updated_beneficiaries += 1
    except Exception:
        logger.exception(
            "[numero_paie_sync] beneficiary update failed individual_id=%s",
            individual.id,
        )

    # Trace légère aussi sur la facture (contexte réconciliation)
    try:
        from payroll.reconciliation_benefit_utils import (
            merge_benefit_json_ext,
            save_benefit_if_dirty,
        )

        if merge_benefit_json_ext(
            benefit,
            {
                "numero_paie_sync": {
                    "action": ACTION_UPDATED,
                    "previous": previous,
                    "new": new_value,
                    "source": source,
                    "at": now,
                }
            },
        ):
            save_benefit_if_dirty(benefit, username)
    except Exception:
        logger.exception(
            "[numero_paie_sync] benefit json_ext tip failed benefit=%s",
            getattr(benefit, "code", None),
        )

    logger.info(
        "[numero_paie_sync] updated benefit=%s previous=%s new=%s source=%s beneficiaries=%s",
        getattr(benefit, "code", None),
        previous,
        new_value,
        source,
        updated_beneficiaries,
    )
    return {
        "action": ACTION_UPDATED,
        "previous": previous,
        "new": new_value,
        "source": source,
        "beneficiaries_updated": updated_beneficiaries,
    }


def sync_numero_paie_from_mapping(benefit, mapping, *, source, username):
    """Convenience : extrait depuis dict/Series puis synchronise."""
    return sync_numero_paie_from_operator(
        benefit,
        extract_numero_paie(mapping),
        source=source,
        username=username,
    )


def _apply_numero_paie_on_json_ext(entity, new_value, history_entry):
    json_ext = dict(entity.json_ext or {})
    history = list(json_ext.get(HISTORY_KEY) or [])
    history.append(history_entry)
    if len(history) > MAX_HISTORY_ENTRIES:
        history = history[-MAX_HISTORY_ENTRIES:]
    json_ext[NUMERO_PAIE_KEY] = new_value
    json_ext[HISTORY_KEY] = history
    entity.json_ext = json_ext


def _save_history_model(entity, username):
    """Sauvegarde via le modèle History* pour conserver HistoricalIndividual / Beneficiary."""
    from payroll.opensearch_payroll_status_sync import skip_heavy_opensearch_reindex

    save_username = username or "System"
    with skip_heavy_opensearch_reindex():
        if hasattr(entity, "save"):
            try:
                entity.save(username=save_username)
            except TypeError:
                entity.save()
