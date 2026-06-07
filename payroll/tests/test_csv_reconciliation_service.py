from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from django.test import SimpleTestCase

from payroll.apps import PayrollConfig
from payroll.models import BenefitConsumptionStatus
from payroll.services import CsvReconciliationService


class CsvReconciliationServiceTests(SimpleTestCase):
    def setUp(self):
        self.service = CsvReconciliationService(
            SimpleNamespace(login_name="tester", username="tester")
        )

    def test_get_additional_columns_maps_legacy_code_empreinte(self):
        with patch.object(
            PayrollConfig,
            "csv_reconciliation_additional_columns",
            ["code_menage", "code_empreinte", "numero_paie"],
            create=True,
        ):
            cols = self.service._get_additional_columns()
        self.assertIn("code_client", cols)
        self.assertNotIn("code_empreinte", cols)

    def test_receipt_required_only_when_paid_oui(self):
        benefit = SimpleNamespace(
            code="BC001",
            status=BenefitConsumptionStatus.ACCEPTED,
            individual=None,
        )
        row_non = pd.Series(
            {
                "code": "BC001",
                PayrollConfig.csv_reconciliation_paid_extra_field: "Non",
                PayrollConfig.csv_reconciliation_receipt_column: float("nan"),
                "status": BenefitConsumptionStatus.ACCEPTED,
            }
        )
        errors_non, _ = self.service._validate_reconciliation_row(
            payroll=SimpleNamespace(),
            row=row_non,
            benefits_by_code={"BC001": benefit},
            payroll_codes={"BC001"},
        )
        self.assertIsNone(errors_non)

        row_oui = pd.Series(
            {
                "code": "BC001",
                PayrollConfig.csv_reconciliation_paid_extra_field: "Oui",
                PayrollConfig.csv_reconciliation_receipt_column: float("nan"),
                "status": BenefitConsumptionStatus.ACCEPTED,
            }
        )
        errors_oui, _ = self.service._validate_reconciliation_row(
            payroll=SimpleNamespace(),
            row=row_oui,
            benefits_by_code={"BC001": benefit},
            payroll_codes={"BC001"},
        )
        self.assertIn("receipt_required", errors_oui)

    def test_reconcile_row_ignores_empty_paid_value(self):
        row = pd.Series(
            {
                "code": "BC001",
                PayrollConfig.csv_reconciliation_paid_extra_field: float("nan"),
                PayrollConfig.csv_reconciliation_receipt_column: "RCPT-1",
                "status": BenefitConsumptionStatus.ACCEPTED,
            }
        )
        benefit = SimpleNamespace(
            code="BC001",
            status=BenefitConsumptionStatus.ACCEPTED,
            individual=None,
        )
        errors = self.service._reconcile_row(
            payroll=SimpleNamespace(),
            row=row,
            benefits_by_code={"BC001": benefit},
            payroll_codes={"BC001"},
        )
        self.assertIsNone(errors)

    def test_optional_columns_removed_if_not_in_schema(self):
        cols = self.service._resolve_optional_additional_columns(
            ["code_menage", "numero_paie", "code_client"],
            observed_schema_keys={"code_menage"},
        )
        self.assertEqual(cols, ["code_menage"])

    def test_optional_columns_kept_if_present_in_schema(self):
        cols = self.service._resolve_optional_additional_columns(
            ["code_menage", "numero_paie", "code_client"],
            observed_schema_keys={"numero_paie", "code_empreinte"},
        )
        self.assertIn("numero_paie", cols)
        self.assertIn("code_client", cols)

    def test_normalize_upload_maps_paye_header_to_internal_paid_column(self):
        df = pd.DataFrame(
            {
                "Code": ["BC001"],
                "Paye": ["Oui"],
                "Recu": ["R1"],
                "Statut": [BenefitConsumptionStatus.ACCEPTED],
            }
        )
        self.service._normalize_upload_dataframe_columns(df)
        self.assertIn(PayrollConfig.csv_reconciliation_paid_extra_field, df.columns)
