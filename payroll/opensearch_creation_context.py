"""
Caches lecture seule pour l'indexation OpenSearch en fin de création de paie.

Évite le N+1 SQL dans documents.prepare() (beneficiary, locations, PBC par facture).
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from django.db.models import QuerySet

logger = __import__("logging").getLogger(__name__)

_payroll_creation_opensearch_context: contextvars.ContextVar[
    Optional["PayrollCreationOpenSearchContext"]
] = contextvars.ContextVar("payroll_creation_opensearch_context", default=None)


@dataclass
class PayrollCreationOpenSearchContext:
    payroll_id: str
    payroll: Any
    benefit_ids: List[Any] = field(default_factory=list)
    benefit_plan_fallback: Optional[Dict[str, Any]] = None
    beneficiaries_by_individual: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    location_by_individual: Dict[str, Optional[Dict[str, Any]]] = field(default_factory=dict)
    payment_plan_codes: List[str] = field(default_factory=list)
    payment_cycle_codes: List[str] = field(default_factory=list)
    payroll_names: List[str] = field(default_factory=list)
    payroll_nested_doc: List[Dict[str, Any]] = field(default_factory=list)


def get_payroll_creation_opensearch_context() -> Optional[PayrollCreationOpenSearchContext]:
    return _payroll_creation_opensearch_context.get()


def use_payroll_creation_opensearch_context(ctx: PayrollCreationOpenSearchContext):
    return _payroll_creation_opensearch_context.set(ctx)


def reset_payroll_creation_opensearch_context(token) -> None:
    _payroll_creation_opensearch_context.reset(token)


def benefit_plan_dict(benefit_plan) -> Optional[Dict[str, Any]]:
    if not benefit_plan:
        return None
    return {
        "id": str(benefit_plan.id) if hasattr(benefit_plan, "id") else None,
        "code": getattr(benefit_plan, "code", None),
        "name": getattr(benefit_plan, "name", None) or str(benefit_plan),
    }


def benefit_plan_for_individual(individual) -> Optional[Dict[str, Any]]:
    """Plan pour un individu : cache création paie, sinon requêtes historiques."""
    if not individual:
        return None

    ctx = get_payroll_creation_opensearch_context()
    if ctx:
        cached = ctx.beneficiaries_by_individual.get(str(individual.id))
        if cached:
            return cached
        return ctx.benefit_plan_fallback

    return _benefit_plan_for_individual_uncached(individual)


def location_for_individual(individual) -> Optional[Dict[str, Any]]:
    if not individual:
        return None

    ctx = get_payroll_creation_opensearch_context()
    if ctx:
        if str(individual.id) in ctx.location_by_individual:
            return ctx.location_by_individual[str(individual.id)]
        return _location_from_json_ext(individual)

    return None


def payment_cycle_fields_from_creation_context():
    """Infos cycle de paiement pour la paie en cours de création."""
    ctx = get_payroll_creation_opensearch_context()
    if not ctx or not ctx.payroll:
        return {}
    cycle = getattr(ctx.payroll, "payment_cycle", None)
    if not cycle:
        return {}
    return {
        "payment_cycle_code": getattr(cycle, "code", None),
        "payment_cycle_start_date": getattr(cycle, "start_date", None),
        "payment_cycle_end_date": getattr(cycle, "end_date", None),
    }


def _benefit_plan_from_payment_plan(payment_plan) -> Optional[Dict[str, Any]]:
    if not payment_plan:
        return None
    try:
        return benefit_plan_dict(payment_plan.benefit_plan)
    except Exception:
        return None


def _benefit_plan_for_individual_uncached(individual):
    try:
        from social_protection.models import Beneficiary, BeneficiaryStatus
    except ImportError:
        return None

    beneficiary = (
        Beneficiary.objects.filter(
            individual=individual,
            status=BeneficiaryStatus.ACTIVE,
            is_deleted=False,
        )
        .select_related("benefit_plan")
        .order_by("-date_valid_from")
        .first()
    )
    if not beneficiary:
        beneficiary = (
            Beneficiary.objects.filter(individual=individual, is_deleted=False)
            .select_related("benefit_plan")
            .order_by("-date_valid_from")
            .first()
        )
    if beneficiary and beneficiary.benefit_plan:
        return benefit_plan_dict(beneficiary.benefit_plan)
    return None


def _location_from_json_ext(individual) -> Optional[Dict[str, Any]]:
    json_ext = getattr(individual, "json_ext", None) or {}
    if not json_ext:
        return None
    return {
        "region": json_ext.get("region"),
        "prefecture": json_ext.get("prefecture"),
        "sous_prefecture": json_ext.get("sous_prefecture"),
        "district": json_ext.get("district"),
    }


def _load_locations_with_ancestors(root_ids: Set[int]) -> Dict[int, Any]:
    from location.models import Location

    location_by_id: Dict[int, Any] = {}
    pending = {lid for lid in root_ids if lid}
    while pending:
        to_fetch = pending - set(location_by_id.keys())
        if not to_fetch:
            break
        pending = set()
        for loc in Location.objects.filter(id__in=to_fetch):
            location_by_id[loc.id] = loc
            if loc.parent_id and loc.parent_id not in location_by_id:
                pending.add(loc.parent_id)
    return location_by_id


def _format_location_hierarchy(location, location_by_id: Dict[int, Any]) -> Dict[str, Any]:
    data = {
        "code": location.code,
        "name": location.name,
        "type": location.type,
    }
    current = location
    while current:
        loc_type = getattr(current, "type", None)
        if loc_type == "R":
            data.setdefault("region", current.name)
            data.setdefault("prefecture", current.name)
        elif loc_type == "P":
            data.setdefault("prefecture", current.name)
        elif loc_type == "S":
            data.setdefault("sous_prefecture", current.name)
        elif loc_type == "D":
            data.setdefault("district", current.name)
        elif loc_type == "W":
            data.setdefault("sous_prefecture", current.name)
        parent_id = getattr(current, "parent_id", None)
        current = location_by_id.get(parent_id) if parent_id else None
    return data


def _resolve_location_for_individual(
    individual_id: str,
    individual_location_id: Optional[int],
    group_location_id: Optional[int],
    json_ext: Optional[dict],
    location_by_id: Dict[int, Any],
) -> Optional[Dict[str, Any]]:
    loc_id = individual_location_id or group_location_id
    if loc_id and loc_id in location_by_id:
        return _format_location_hierarchy(location_by_id[loc_id], location_by_id)
    if json_ext:
        return {
            "region": json_ext.get("region"),
            "prefecture": json_ext.get("prefecture"),
            "sous_prefecture": json_ext.get("sous_prefecture"),
            "district": json_ext.get("district"),
        }
    return None


def _build_beneficiary_cache(individual_ids: Set[Any], fallback: Optional[Dict[str, Any]]):
    if not individual_ids:
        return {}

    try:
        from social_protection.models import Beneficiary, BeneficiaryStatus
    except ImportError:
        return {}

    cache: Dict[str, Dict[str, Any]] = {}

    active_qs = (
        Beneficiary.objects.filter(
            individual_id__in=individual_ids,
            is_deleted=False,
            status=BeneficiaryStatus.ACTIVE,
        )
        .select_related("benefit_plan")
        .order_by("individual_id", "-date_valid_from")
    )
    for beneficiary in active_qs.iterator(chunk_size=2000):
        iid = str(beneficiary.individual_id)
        if iid in cache:
            continue
        plan = benefit_plan_dict(beneficiary.benefit_plan)
        if plan:
            cache[iid] = plan

    missing = {iid for iid in (str(x) for x in individual_ids)} - set(cache.keys())
    if missing:
        other_qs = (
            Beneficiary.objects.filter(individual_id__in=missing, is_deleted=False)
            .select_related("benefit_plan")
            .order_by("individual_id", "-date_valid_from")
        )
        for beneficiary in other_qs.iterator(chunk_size=2000):
            iid = str(beneficiary.individual_id)
            if iid in cache:
                continue
            plan = benefit_plan_dict(beneficiary.benefit_plan)
            if plan:
                cache[iid] = plan

    if fallback:
        for iid in (str(x) for x in individual_ids):
            cache.setdefault(iid, fallback)

    return cache


def _build_location_cache(individual_ids: Set[Any]) -> Dict[str, Optional[Dict[str, Any]]]:
    if not individual_ids:
        return {}

    try:
        from individual.models import GroupIndividual, Individual
    except ImportError:
        return {}

    individuals = {
        str(row["id"]): row
        for row in Individual.objects.filter(id__in=individual_ids).values(
            "id", "location_id", "json_ext"
        )
    }

    group_location_by_individual: Dict[str, int] = {}
    for row in (
        GroupIndividual.objects.filter(individual_id__in=individual_ids, is_deleted=False)
        .select_related("group")
        .values("individual_id", "group__location_id")
    ):
        loc_id = row.get("group__location_id")
        if loc_id:
            group_location_by_individual[str(row["individual_id"])] = loc_id

    root_ids: Set[int] = set()
    for iid, row in individuals.items():
        loc_id = row.get("location_id") or group_location_by_individual.get(iid)
        if loc_id:
            root_ids.add(loc_id)

    location_by_id = _load_locations_with_ancestors(root_ids)

    cache: Dict[str, Optional[Dict[str, Any]]] = {}
    for iid in (str(x) for x in individual_ids):
        row = individuals.get(iid, {})
        cache[iid] = _resolve_location_for_individual(
            iid,
            row.get("location_id"),
            group_location_by_individual.get(iid),
            row.get("json_ext") or {},
            location_by_id,
        )
    return cache


def _payroll_nested_document(payroll) -> List[Dict[str, Any]]:
    payment_plan = getattr(payroll, "payment_plan", None)
    payment_cycle = getattr(payroll, "payment_cycle", None)
    return [
        {
            "name": payroll.name,
            "status": payroll.status,
            "payment_method": payroll.payment_method,
            "date_created": payroll.date_created,
            "payment_plan": (
                {
                    "code": payment_plan.code,
                    "name": payment_plan.name,
                }
                if payment_plan
                else None
            ),
            "payment_cycle": (
                {
                    "code": payment_cycle.code,
                    "status": payment_cycle.status,
                    "start_date": payment_cycle.start_date,
                    "end_date": payment_cycle.end_date,
                }
                if payment_cycle
                else None
            ),
        }
    ]


def build_payroll_creation_opensearch_context(payroll) -> PayrollCreationOpenSearchContext:
    from payroll.models import PayrollBenefitConsumption

    payroll_id = str(payroll.id)
    payment_plan = getattr(payroll, "payment_plan", None)
    payment_cycle = getattr(payroll, "payment_cycle", None)

    benefit_ids: List[Any] = []
    individual_ids: Set[Any] = set()

    for benefit_id, individual_id in (
        PayrollBenefitConsumption.objects.filter(payroll_id=payroll_id, is_deleted=False)
        .values_list("benefit_id", "benefit__individual_id")
        .iterator(chunk_size=5000)
    ):
        benefit_ids.append(benefit_id)
        if individual_id:
            individual_ids.add(individual_id)

    fallback = _benefit_plan_from_payment_plan(payment_plan)

    ctx = PayrollCreationOpenSearchContext(
        payroll_id=payroll_id,
        payroll=payroll,
        benefit_ids=benefit_ids,
        benefit_plan_fallback=fallback,
        beneficiaries_by_individual=_build_beneficiary_cache(individual_ids, fallback),
        location_by_individual=_build_location_cache(individual_ids),
        payment_plan_codes=(
            [payment_plan.code] if payment_plan and payment_plan.code else []
        ),
        payment_cycle_codes=(
            [payment_cycle.code] if payment_cycle and payment_cycle.code else []
        ),
        payroll_names=([payroll.name] if payroll.name else []),
        payroll_nested_doc=_payroll_nested_document(payroll),
    )

    logger.info(
        "OpenSearch creation context payroll_id=%s benefits=%s individuals=%s",
        payroll_id,
        len(benefit_ids),
        len(individual_ids),
    )
    return ctx


def pbc_queryset_for_creation(payroll_id) -> QuerySet:
    from payroll.models import PayrollBenefitConsumption

    return PayrollBenefitConsumption.objects.filter(
        payroll_id=payroll_id,
        is_deleted=False,
    ).select_related(
        "payroll",
        "payroll__payment_plan",
        "payroll__payment_cycle",
        "benefit",
        "benefit__individual",
        "benefit__individual__location",
    )
