from rest_framework.permissions import BasePermission

from payroll.apps import PayrollConfig


class ReconciliationCallbackAPIKeyPermission(BasePermission):
    """
    Authentification par clé API pour le callback opérateur (Bearer ou X-Api-Key).
    """

    message = "Invalid or missing reconciliation callback API key"

    def has_permission(self, request, view):
        expected_key = PayrollConfig.reconciliation_callback_api_key
        if not expected_key:
            return False
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            provided = auth_header[7:].strip()
        else:
            provided = request.headers.get("X-Api-Key", "").strip()
        return bool(provided) and provided == expected_key.strip()
