"""Payload partagé entre l'appel pull (passerelle) et le callback push (opérateur)."""

RECONCILIATION_PAYLOAD_FIELDS = (
    "invoiceId",
    "amount",
    "projet",
    "campagne",
    "codeMenage",
)


def build_reconciliation_payload(invoice_id, amount, projet=None, campagne=None, code_menage=None):
    return {
        "invoiceId": str(invoice_id),
        "amount": str(amount),
        "projet": str(projet) if projet is not None else "",
        "campagne": str(campagne) if campagne is not None else "",
        "codeMenage": str(code_menage) if code_menage is not None else "",
    }


def parse_reconciliation_payload(data):
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object")
    payload = {}
    for field in RECONCILIATION_PAYLOAD_FIELDS:
        if field not in data or data[field] is None:
            raise ValueError(f"{field} is required")
        payload[field] = str(data[field])
    return payload


def _parse_bool(value):
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in ("true", "1", "yes"):
        return True
    if normalized in ("false", "0", "no"):
        return False
    raise ValueError("success must be true or false")


def parse_reconciliation_callback_payload(data):
    """
    Payload push opérateur : champs d'identification (comme le pull)
    + success (équivalent réponse true/false) + receipt/transactionId si succès.
    """
    payload = parse_reconciliation_payload(data)
    if "success" not in data or data["success"] is None:
        raise ValueError("success is required")
    payload["success"] = _parse_bool(data["success"])

    receipt = data.get("receipt") or data.get("transactionId")
    if receipt is not None:
        receipt = str(receipt).strip()
    payload["receipt"] = receipt or None

    if payload["success"] and not payload["receipt"]:
        raise ValueError("receipt or transactionId is required when success is true")

    return payload


def _parse_bool_field(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in ("true", "1", "yes"):
        return True
    if normalized in ("false", "0", "no"):
        return False
    return None


def _extract_receipt_from_gateway_data(data):
    if not isinstance(data, dict):
        return None
    for key in ("transactionId", "receipt", "reference"):
        value = data.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def parse_reconciliation_gateway_response(response):
    """
    Interprète la réponse HTTP pull de la passerelle (/reconciliation).
    Formats supportés :
    - texte brut : true / false
    - JSON : { success: true } ou { reconciled: true, transactionId: "..." }
    """
    if not response:
        return {"success": False, "receipt": None}

    response_text = response.text.strip()
    lowered = response_text.lower()
    if lowered == "true":
        return {"success": True, "receipt": None}
    if lowered == "false":
        return {"success": False, "receipt": None}

    try:
        data = response.json()
    except ValueError:
        data = None

    if isinstance(data, dict):
        receipt = _extract_receipt_from_gateway_data(data)
        for field in ("success", "reconciled"):
            if field in data:
                parsed = _parse_bool_field(data.get(field))
                if parsed is True:
                    return {"success": True, "receipt": receipt}
                if parsed is False:
                    return {"success": False, "receipt": None}

    return {"success": False, "receipt": None}


def normalize_reconciliation_gateway_result(result):
    """Compatibilité si un connecteur renvoie encore un booléen."""
    if isinstance(result, dict):
        return result
    if result is True:
        return {"success": True, "receipt": None}
    return {"success": False, "receipt": None}
