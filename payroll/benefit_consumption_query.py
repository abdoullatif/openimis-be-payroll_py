"""Requêtes benefitConsumption par paie (pagination GraphQL, anti N+1)."""

from django.db.models import Count, Prefetch, Q

from payroll.models import BenefitAttachment, BenefitConsumption


def benefit_consumption_filters_for_payroll(payroll_uuid, *, status=None, filter_only_unpaid=False):
    filters = [
        Q(
            payrollbenefitconsumption__payroll_id=payroll_uuid,
            is_deleted=False,
            payrollbenefitconsumption__is_deleted=False,
            payrollbenefitconsumption__payroll__is_deleted=False,
        )
    ]
    if status:
        filters.append(Q(status=status))
    if filter_only_unpaid:
        from payroll.models import BenefitConsumptionStatus

        filters.append(
            Q(
                status__in=[
                    BenefitConsumptionStatus.ACCEPTED,
                    BenefitConsumptionStatus.APPROVE_FOR_PAYMENT,
                ]
            )
        )
    return filters


def benefit_consumption_queryset_for_payroll(
    payroll_uuid,
    *,
    status=None,
    filter_only_unpaid=False,
):
    attachment_qs = BenefitAttachment.objects.filter(is_deleted=False).select_related("bill")
    return (
        BenefitConsumption.objects.filter(
            *benefit_consumption_filters_for_payroll(
                payroll_uuid,
                status=status,
                filter_only_unpaid=filter_only_unpaid,
            )
        )
        .select_related("individual")
        .prefetch_related(Prefetch("benefitattachment_set", queryset=attachment_qs))
    )


def count_benefit_consumption_for_payroll(payroll_uuid, *, status=None, filter_only_unpaid=False):
    return benefit_consumption_queryset_for_payroll(
        payroll_uuid,
        status=status,
        filter_only_unpaid=filter_only_unpaid,
    ).count()


def benefit_consumption_status_counts_for_payroll(payroll_uuid):
    """Compteurs par statut pour les onglets (évite de charger la liste entière)."""
    rows = (
        BenefitConsumption.objects.filter(
            *benefit_consumption_filters_for_payroll(payroll_uuid)
        )
        .values("status")
        .annotate(total=Count("id"))
    )
    return {row["status"]: row["total"] for row in rows}
