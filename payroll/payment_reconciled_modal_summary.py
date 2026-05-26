"""Alias modale « Paiements réconciliés » — mêmes indicateurs que paiements approuvés."""

from payroll.payment_approved_modal_summary import build_payment_approved_modal_summary


def build_payment_reconciled_modal_summary(payroll):
    return build_payment_approved_modal_summary(payroll)
