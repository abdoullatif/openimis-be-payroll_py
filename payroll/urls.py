from django.urls import path

from payroll.views import (
    send_callback_to_openimis,
    reconciliation_callback_from_operator,
    CSVReconciliationAPIView,
    PaymentReportAPIView,
)

urlpatterns = [
    path('send_callback_to_openimis/', send_callback_to_openimis),
    path('reconciliation_callback/', reconciliation_callback_from_operator),
    path('reconciliation-callback/', reconciliation_callback_from_operator),
    path('csv_reconciliation/', CSVReconciliationAPIView.as_view()),
    # alias with hyphen for frontend compatibility
    path('csv-reconciliation/', CSVReconciliationAPIView.as_view()),
    path('payment_reports/', PaymentReportAPIView.as_view()),
]
