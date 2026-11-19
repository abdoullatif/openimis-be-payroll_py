from django.test import SimpleTestCase

from payroll.reconciliation_payload import (
    build_reconciliation_payload,
    parse_reconciliation_callback_payload,
    parse_reconciliation_payload,
)


class ReconciliationPayloadIdBenPrincipalTests(SimpleTestCase):
    def test_build_uses_id_ben_principal(self):
        payload = build_reconciliation_payload(
            "B-001",
            "1000",
            projet="TM",
            campagne="PAIE-1",
            id_ben_principal="7MF-9YN-9MK",
        )
        self.assertEqual(payload["id_ben_principal"], "7MF-9YN-9MK")
        self.assertNotIn("codeMenage", payload)

    def test_parse_requires_id_ben_principal(self):
        parsed = parse_reconciliation_payload({
            "invoiceId": "B-001",
            "amount": "1000",
            "projet": "TM",
            "campagne": "PAIE-1",
            "id_ben_principal": "7MF-9YN-9MK",
        })
        self.assertEqual(parsed["id_ben_principal"], "7MF-9YN-9MK")

    def test_parse_accepts_legacy_codeMenage(self):
        parsed = parse_reconciliation_payload({
            "invoiceId": "B-001",
            "amount": "1000",
            "projet": "TM",
            "campagne": "PAIE-1",
            "codeMenage": "LEGACY",
        })
        self.assertEqual(parsed["id_ben_principal"], "LEGACY")

    def test_callback_success_requires_receipt(self):
        with self.assertRaises(ValueError):
            parse_reconciliation_callback_payload({
                "invoiceId": "B-001",
                "amount": "1000",
                "projet": "TM",
                "campagne": "PAIE-1",
                "id_ben_principal": "7MF-9YN-9MK",
                "success": True,
            })

    def test_callback_success_with_transaction_id(self):
        parsed = parse_reconciliation_callback_payload({
            "invoiceId": "B-001",
            "amount": "1000",
            "projet": "TM",
            "campagne": "PAIE-1",
            "id_ben_principal": "7MF-9YN-9MK",
            "success": True,
            "transactionId": "OP-123",
        })
        self.assertTrue(parsed["success"])
        self.assertEqual(parsed["receipt"], "OP-123")
