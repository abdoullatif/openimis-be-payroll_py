from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from payroll.payment_progress import (
    STATUS_CANCELLED,
    STATUS_IN_PROGRESS,
    STATUS_STALE,
    reconcile_payment_progress_for_payroll,
)


class PaymentProgressStaleRecoveryTests(TestCase):
    def _payroll_stub(self, *, payment_in_progress=True, payment_progress=None):
        class Payroll:
            id = "00000000-0000-0000-0000-000000000001"
            json_ext = {
                "payment_in_progress": payment_in_progress,
                "payment_progress": payment_progress or {},
            }

        return Payroll()

    @patch("payroll.payment_progress._release_payment_in_progress_lock")
    @patch("payroll.payment_progress.persist_payment_progress_to_payroll")
    @patch("payroll.payment_progress.cache")
    def test_stale_recovered_while_payment_flag_active(self, mock_cache, mock_persist, _mock_release):
        mock_cache.get.return_value = None
        recent = (timezone.now() - timedelta(minutes=2)).isoformat()
        payroll = self._payroll_stub(
            payment_progress={
                "status": STATUS_STALE,
                "phase": "STALE",
                "should_stop_polling": True,
                "processed_beneficiaries": 50000,
                "total_beneficiaries": 119000,
                "updated_at": recent,
            }
        )

        result = reconcile_payment_progress_for_payroll(payroll)

        self.assertEqual(result["status"], STATUS_IN_PROGRESS)
        self.assertFalse(result["should_stop_polling"])
        mock_cache.set.assert_called_once()
        mock_persist.assert_called_once()

    @patch("payroll.payment_progress._release_payment_in_progress_lock")
    @patch("payroll.payment_progress.cache")
    def test_in_progress_not_marked_stale_while_flag_active(self, mock_cache, _mock_release):
        recent = (timezone.now() - timedelta(minutes=2)).isoformat()
        payroll = self._payroll_stub(
            payment_progress={
                "status": STATUS_IN_PROGRESS,
                "phase": "GATEWAY_PAYMENT",
                "started_at": recent,
                "updated_at": recent,
                "processed_beneficiaries": 90000,
                "total_beneficiaries": 119000,
            }
        )
        mock_cache.get.return_value = None

        result = reconcile_payment_progress_for_payroll(payroll)

        self.assertEqual(result["status"], STATUS_IN_PROGRESS)
        self.assertFalse(result["should_stop_polling"])

    @patch("payroll.payment_progress.cache")
    def test_in_progress_kept_when_flag_not_yet_set_but_recent(self, mock_cache):
        recent = (timezone.now() - timedelta(minutes=2)).isoformat()
        payroll = self._payroll_stub(
            payment_in_progress=False,
            payment_progress={
                "status": STATUS_IN_PROGRESS,
                "phase": "GATEWAY_PAYMENT",
                "started_at": recent,
                "updated_at": recent,
                "processed_beneficiaries": 10,
                "total_beneficiaries": 119000,
            },
        )
        mock_cache.get.return_value = None

        result = reconcile_payment_progress_for_payroll(payroll)

        self.assertEqual(result["status"], STATUS_IN_PROGRESS)
        self.assertFalse(result["should_stop_polling"])

    @patch("payroll.payment_progress._release_payment_in_progress_lock")
    @patch("payroll.payment_progress.cache")
    def test_cancelled_clears_orphan_flag_when_worker_stopped(self, mock_cache, mock_release):
        mock_cache.get.return_value = None
        payroll = self._payroll_stub(
            payment_progress={
                "status": STATUS_CANCELLED,
                "message": "Payment cancelled or job interrupted.",
                "processed_beneficiaries": 100,
                "total_beneficiaries": 119000,
            }
        )

        result = reconcile_payment_progress_for_payroll(payroll)

        self.assertEqual(result["status"], STATUS_CANCELLED)
        self.assertTrue(result["should_stop_polling"])
        mock_release.assert_called_once()
