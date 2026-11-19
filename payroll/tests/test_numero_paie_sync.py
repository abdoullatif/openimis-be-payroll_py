from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from payroll.numero_paie_sync import (
    ACTION_SKIPPED_EMPTY,
    ACTION_UNCHANGED,
    ACTION_UPDATED,
    SOURCE_OPERATOR_CALLBACK,
    extract_numero_paie,
    sync_numero_paie_from_operator,
)
from payroll.reconciliation_payload import (
    parse_reconciliation_callback_payload,
    parse_reconciliation_gateway_response,
)


class ExtractNumeroPaieTests(SimpleTestCase):
    def test_extract_camel_and_snake(self):
        self.assertEqual(extract_numero_paie({"numeroPaie": " 224-01 "}), "224-01")
        self.assertEqual(extract_numero_paie({"numero_paie": "ABC"}), "ABC")
        self.assertIsNone(extract_numero_paie({"numeroPaie": ""}))
        self.assertIsNone(extract_numero_paie({"receipt": "R1"}))

    def test_normalize_excel_float_string(self):
        self.assertEqual(extract_numero_paie({"numero_paie": "12345.0"}), "12345")


class SyncNumeroPaieTests(SimpleTestCase):
    def _benefit(self, stored=None):
        individual = MagicMock()
        individual.id = "ind-1"
        individual.json_ext = {"numero_paie": stored} if stored else {}
        individual.is_deleted = False
        benefit = MagicMock()
        benefit.code = "INV-1"
        benefit.individual = individual
        benefit.individual_id = "ind-1"
        benefit.json_ext = {}
        return benefit, individual

    def test_skip_empty(self):
        benefit, _ = self._benefit("OLD")
        result = sync_numero_paie_from_operator(
            benefit, None, source=SOURCE_OPERATOR_CALLBACK, username="Admin"
        )
        self.assertEqual(result["action"], ACTION_SKIPPED_EMPTY)

    def test_unchanged(self):
        benefit, _ = self._benefit("SAME")
        result = sync_numero_paie_from_operator(
            benefit, "SAME", source=SOURCE_OPERATOR_CALLBACK, username="Admin"
        )
        self.assertEqual(result["action"], ACTION_UNCHANGED)

    @patch("payroll.numero_paie_sync._save_history_model")
    @patch("social_protection.models.Beneficiary")
    @patch("payroll.reconciliation_benefit_utils.save_benefit_if_dirty")
    def test_updated_writes_history(self, _save_benefit, mock_ben_model, mock_save):
        benefit, individual = self._benefit("OLD-001")
        mock_ben_model.objects.filter.return_value = []

        result = sync_numero_paie_from_operator(
            benefit,
            "NEW-002",
            source=SOURCE_OPERATOR_CALLBACK,
            username="Admin",
            invoice_id="INV-1",
        )

        self.assertEqual(result["action"], ACTION_UPDATED)
        self.assertEqual(result["previous"], "OLD-001")
        self.assertEqual(result["new"], "NEW-002")
        self.assertEqual(individual.json_ext["numero_paie"], "NEW-002")
        history = individual.json_ext["numero_paie_history"]
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["previous"], "OLD-001")
        self.assertEqual(history[0]["new"], "NEW-002")
        self.assertEqual(history[0]["source"], SOURCE_OPERATOR_CALLBACK)
        mock_save.assert_called()


class CallbackPayloadNumeroPaieTests(SimpleTestCase):
    def test_callback_parses_optional_numero_paie(self):
        parsed = parse_reconciliation_callback_payload({
            "invoiceId": "B-001",
            "amount": "1000",
            "projet": "TM",
            "campagne": "PAIE-1",
            "id_ben_principal": "7MF-9YN-9MK",
            "success": True,
            "transactionId": "OP-123",
            "numeroPaie": "NP-99",
        })
        self.assertEqual(parsed["numero_paie"], "NP-99")
        self.assertEqual(parsed["receipt"], "OP-123")

    def test_callback_without_numero_paie(self):
        parsed = parse_reconciliation_callback_payload({
            "invoiceId": "B-001",
            "amount": "1000",
            "projet": "TM",
            "campagne": "PAIE-1",
            "id_ben_principal": "7MF-9YN-9MK",
            "success": True,
            "receipt": "OP-123",
        })
        self.assertIsNone(parsed["numero_paie"])


class GatewayResponseNumeroPaieTests(SimpleTestCase):
    def test_json_success_with_numero_paie(self):
        response = MagicMock()
        response.text = '{"success": true, "transactionId": "T1", "numeroPaie": "NP-7"}'
        response.json.return_value = {
            "success": True,
            "transactionId": "T1",
            "numeroPaie": "NP-7",
        }
        parsed = parse_reconciliation_gateway_response(response)
        self.assertTrue(parsed["success"])
        self.assertEqual(parsed["receipt"], "T1")
        self.assertEqual(parsed["numero_paie"], "NP-7")
