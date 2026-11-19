from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from payroll.notifications import (
    notify_payment_finished,
    notify_reconciliation_finished,
)


class PayrollNotificationsTests(SimpleTestCase):
    def _payroll(self):
        p = MagicMock()
        p.id = "11111111-1111-1111-1111-111111111111"
        p.name = "PAIE-TEST"
        p.user_created = None
        return p

    def _user(self):
        u = MagicMock()
        u.id = 1
        u.email = "op@example.com"
        u.i_user = None
        u.login_name = "Operateur"
        return u

    @patch("payroll.notifications.is_task_email_enabled", return_value=True)
    @patch("payroll.notifications.schedule_task_emails")
    @patch("payroll.notifications.get_user_email", return_value="op@example.com")
    @patch("payroll.notifications.get_user_display_name", return_value="Operateur")
    def test_notify_payment_finished(self, _name, _email, mock_sched, _enabled):
        sent = notify_payment_finished(
            self._payroll(),
            self._user(),
            {"status": "COMPLETED", "success_count": 10, "rejected_count": 2,
             "total_beneficiaries": 12, "processed_beneficiaries": 12},
        )
        self.assertEqual(sent, 1)
        mock_sched.assert_called_once()
        msg = mock_sched.call_args[0][0][0]
        self.assertIn("Paiement terminé", msg["subject"])
        self.assertIn("Succès : 10", msg["message"])

    @patch("payroll.notifications.is_task_email_enabled", return_value=True)
    @patch("payroll.notifications.schedule_task_emails")
    @patch("payroll.notifications.get_user_email", return_value="op@example.com")
    @patch("payroll.notifications.get_user_display_name", return_value="Operateur")
    def test_notify_reconciliation_finished(self, _name, _email, mock_sched, _enabled):
        sent = notify_reconciliation_finished(
            self._payroll(),
            self._user(),
            {"status": "COMPLETED", "success_count": 5, "rejected_count": 1,
             "total_beneficiaries": 6, "processed_beneficiaries": 6},
        )
        self.assertEqual(sent, 1)
        self.assertIn("Réconciliation terminée", mock_sched.call_args[0][0][0]["subject"])
