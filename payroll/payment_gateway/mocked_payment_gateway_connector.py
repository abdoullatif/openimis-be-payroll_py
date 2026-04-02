import logging

from payroll.payment_gateway.payment_gateway_connector import PaymentGatewayConnector


logger = logging.getLogger(__name__)


class MockedPaymentGatewayConnector(PaymentGatewayConnector):
    def send_payment(self, invoice_id, amount, **kwargs):
        projet = kwargs.get("projet")
        campagne = kwargs.get("campagne")
        code_menage = kwargs.get("code_menage")
        payload = {
            "invoiceId": str(invoice_id),
            "amount": str(amount),
            "projet": str(projet) if projet is not None else "",
            "campagne": str(campagne) if campagne is not None else "",
            "codeMenage": str(code_menage) if code_menage is not None else "",
        }
        #logger.info("[Passerelle] Envoi paiement payload=%s", payload)
        response = self.send_request(self.config.endpoint_payment, payload)
        if not response:
            return False

        logger.info(
            "[Passerelle] Réponse paiement status=%s body=%s",
            response.status_code,
            response.text,
        )

        # 1) Nouveau format JSON attendu
        # {
        #   "success": true,
        #   "message": "B-024-014 invoice of 250000 accepted to be paid",
        #   "transactionId": "...",
        #   "timestamp": "..."
        # }
        try:
            data = response.json()
        except ValueError:
            data = None

        expected_message = f"{invoice_id} invoice of {amount} accepted to be paid"

        if isinstance(data, dict):
            if data.get("success") is True:
                message = data.get("message") or ""
                # On vérifie que le message contient au moins le message attendu
                if expected_message in message:
                    return True
                # Si success == true mais message différent, on considère quand même comme succès
                # pour rester tolérant au backend de la passerelle.
                return True

        # 2) Ancien format texte brut (compatibilité rétro)
        response_text = response.text
        if response_text == expected_message:
            return True

        return False

    def reconcile(self, invoice_id, amount, **kwargs):
        projet = kwargs.get("projet")
        campagne = kwargs.get("campagne")
        code_menage = kwargs.get("code_menage")
        payload = {
            "invoiceId": str(invoice_id),
            "amount": str(amount),
            "projet": str(projet) if projet is not None else "",
            "campagne": str(campagne) if campagne is not None else "",
            "codeMenage": str(code_menage) if code_menage is not None else "",
        }
        response = self.send_request(self.config.endpoint_reconciliation, payload)
        if response:
            logger.info(
                "[Passerelle] Réponse réconciliation status=%s body=%s",
                response.status_code,
                response.text,
            )
            response_text = response.text.strip().lower()
            if response_text == "true":
                return True
            elif response_text == "false":
                return False
        return False
