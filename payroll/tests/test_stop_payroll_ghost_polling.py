from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import SimpleTestCase

from payroll.management.commands.stop_payroll_ghost_polling import Command


class StopPayrollGhostPollingCommandTests(SimpleTestCase):
    def _payroll(self, *, payroll_id="11111111-1111-1111-1111-111111111111", name="PAIE-TEST", json_ext=None):
        payroll = MagicMock()
        payroll.id = payroll_id
        payroll.name = name
        payroll.json_ext = json_ext or {}
        payroll.is_deleted = False
        return payroll

    def test_diagnose_detects_creation_indexing_payment_reconciliation(self):
        payroll = self._payroll(
            json_ext={
                "creation_progress": {"status": "IN_PROGRESS"},
                "opensearch_indexing_progress": {"status": "IN_PROGRESS"},
                "payment_in_progress": True,
                "payment_progress": {"status": "IN_PROGRESS"},
                "reconciliation_in_progress": True,
                "reconciliation_progress": {"status": "IN_PROGRESS"},
            }
        )
        cmd = Command()
        out = StringIO()
        cmd.stdout = out

        with patch(
            "payroll.management.commands.stop_payroll_ghost_polling.get_payroll_creation_progress",
            return_value={"status": "IN_PROGRESS"},
        ), patch(
            "payroll.opensearch_indexing_progress.is_opensearch_indexing_in_progress",
            return_value=True,
        ), patch(
            "payroll.opensearch_indexing_progress.get_opensearch_indexing_progress_for_payroll",
            return_value={"status": "IN_PROGRESS"},
        ), patch(
            "payroll.management.commands.stop_payroll_ghost_polling.is_payment_in_progress",
            return_value=True,
        ), patch(
            "payroll.management.commands.stop_payroll_ghost_polling.get_payroll_payment_progress",
            return_value={"status": "IN_PROGRESS"},
        ), patch(
            "payroll.management.commands.stop_payroll_ghost_polling.is_reconciliation_in_progress",
            return_value=True,
        ), patch(
            "payroll.management.commands.stop_payroll_ghost_polling.get_payroll_reconciliation_progress",
            return_value={"status": "IN_PROGRESS"},
        ):
            actions = cmd._handle_payroll(
                payroll,
                user=MagicMock(),
                username="Admin",
                reason="test",
                diagnose=True,
            )

        self.assertEqual(actions, 1)
        text = out.getvalue()
        self.assertIn("creation=True", text)
        self.assertIn("indexing=True", text)
        self.assertIn("payment=True", text)
        self.assertIn("reconciliation=True", text)

    def test_cleanup_stops_creation_and_indexing(self):
        payroll = self._payroll(
            json_ext={
                "creation_progress": {"status": "IN_PROGRESS"},
                "opensearch_indexing_in_progress": True,
                "opensearch_indexing_progress": {"status": "IN_PROGRESS"},
            }
        )
        cmd = Command()
        cancelled = {"status": "CANCELLED", "should_stop_polling": True}

        with patch(
            "payroll.management.commands.stop_payroll_ghost_polling.get_payroll_creation_progress",
            return_value={"status": "IN_PROGRESS"},
        ), patch(
            "payroll.opensearch_indexing_progress.is_opensearch_indexing_in_progress",
            return_value=True,
        ), patch(
            "payroll.opensearch_indexing_progress.get_opensearch_indexing_progress_for_payroll",
            return_value={"status": "IN_PROGRESS"},
        ), patch(
            "payroll.management.commands.stop_payroll_ghost_polling.is_payment_in_progress",
            return_value=False,
        ), patch(
            "payroll.management.commands.stop_payroll_ghost_polling.get_payroll_payment_progress",
            return_value=None,
        ), patch(
            "payroll.management.commands.stop_payroll_ghost_polling.is_reconciliation_in_progress",
            return_value=False,
        ), patch(
            "payroll.management.commands.stop_payroll_ghost_polling.get_payroll_reconciliation_progress",
            return_value=None,
        ), patch(
            "payroll.management.commands.stop_payroll_ghost_polling.cancel_payroll_creation_progress",
            return_value=cancelled,
        ) as mock_cancel_creation, patch(
            "payroll.management.commands.stop_payroll_ghost_polling.persist_creation_progress_to_payroll",
        ) as mock_persist_creation, patch(
            "payroll.opensearch_indexing_progress.fail_payroll_opensearch_indexing",
        ) as mock_fail_indexing, patch(
            "payroll.management.commands.stop_payroll_ghost_polling.close_stale_payroll_mutations_for_payroll",
            return_value=0,
        ):
            actions = cmd._handle_payroll(
                payroll,
                user=MagicMock(),
                username="Admin",
                reason="Ghost polling cleanup.",
                diagnose=False,
            )

        self.assertEqual(actions, 2)
        mock_cancel_creation.assert_called_once()
        mock_persist_creation.assert_called_once()
        mock_fail_indexing.assert_called_once()

    def test_handle_no_indicators_message(self):
        out = StringIO()
        with patch(
            "payroll.management.commands.stop_payroll_ghost_polling.User.objects.filter"
        ) as mock_user_filter, patch(
            "payroll.management.commands.stop_payroll_ghost_polling.Payroll.objects.filter"
        ) as mock_payroll_filter, patch(
            "payroll.management.commands.stop_payroll_ghost_polling.close_completed_creation_mutations",
            return_value=0,
        ), patch.object(Command, "_handle_orphan_creation_mutations", return_value=0):
            mock_user_filter.return_value.first.return_value = MagicMock(username="Admin")
            mock_payroll_filter.return_value = []
            call_command("stop_payroll_ghost_polling", stdout=out)

        self.assertIn("No ghost polling indicators found", out.getvalue())

    def test_handle_diagnose_flag_via_call_command(self):
        payroll = self._payroll(name="PAIE-DIAG")
        out = StringIO()
        with patch(
            "payroll.management.commands.stop_payroll_ghost_polling.User.objects.filter"
        ) as mock_user_filter, patch(
            "payroll.management.commands.stop_payroll_ghost_polling.Payroll.objects.filter"
        ) as mock_payroll_filter, patch.object(
            Command, "_handle_payroll", return_value=1
        ) as mock_handle, patch.object(
            Command, "_handle_orphan_creation_mutations", return_value=0
        ):
            mock_user_filter.return_value.first.return_value = MagicMock(username="Admin")
            mock_payroll_filter.return_value = [payroll]
            call_command("stop_payroll_ghost_polling", diagnose=True, stdout=out)

        mock_handle.assert_called()
        self.assertNotIn("Stopped ghost polling", out.getvalue())

    def test_orphan_creation_mutation_diagnose_only_lists(self):
        mutation_log = MagicMock()
        mutation_log.id = 99
        mutation_log.client_mutation_id = "cmid-orphan"
        mutation_log.status = "RECEIVED"

        cmd = Command()
        out = StringIO()
        cmd.stdout = out
        with patch(
                "payroll.management.commands.stop_payroll_ghost_polling.MutationLog.objects.filter"
            ) as mock_ml_filter, patch(
                "payroll.management.commands.stop_payroll_ghost_polling._mutation_kind",
                return_value="creation",
            ), patch(
                "payroll.creation_progress.get_payroll_creation_progress_by_mutation",
                return_value={"status": "IN_PROGRESS", "payroll_id": None},
            ):
                mock_ml_filter.return_value.order_by.return_value.__getitem__.return_value = [
                    mutation_log
                ]
                actions = cmd._handle_orphan_creation_mutations(reason="test", diagnose=True)

        self.assertEqual(actions, 1)
        self.assertIn("orphan creation mutation", out.getvalue())
