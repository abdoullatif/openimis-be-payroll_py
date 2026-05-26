"""
Sync OpenSearch léger lors d'un changement de statut paie seul (ex. acceptation).

Évite la ré-indexation en cascade de tous les PayrollBenefitConsumption /
BenefitAttachment (requêtes SQL lourdes par bénéficiaire).
"""

import contextvars
import logging
from contextlib import contextmanager

from django.apps import apps
from django.conf import settings

logger = logging.getLogger(__name__)

_skip_heavy_opensearch_reindex = contextvars.ContextVar(
    "skip_heavy_opensearch_reindex",
    default=False,
)

BULK_CHUNK_SIZE = 500
BENEFIT_STATUS_SYNC_CHUNK = 500


def should_skip_heavy_opensearch_reindex():
    return _skip_heavy_opensearch_reindex.get()


def should_skip_payroll_related_reindex():
    """Alias historique (validation tâche / flags json_ext paie)."""
    return should_skip_heavy_opensearch_reindex()


@contextmanager
def skip_heavy_opensearch_reindex():
    """Désactive ré-indexation OpenSearch lourde (save Payroll / BenefitConsumption)."""
    token = _skip_heavy_opensearch_reindex.set(True)
    try:
        yield
    finally:
        _skip_heavy_opensearch_reindex.reset(token)


@contextmanager
def skip_payroll_related_opensearch_reindex():
    """Alias historique."""
    with skip_heavy_opensearch_reindex():
        yield


def benefit_status_only_save(benefit, username):
    """Save facture sans ré-indexation OpenSearch complète."""
    with skip_heavy_opensearch_reindex():
        benefit.save(username=username)


def payroll_status_only_save(payroll, user):
    """
    Sauvegarde paie + sync OpenSearch ciblée (sans ré-indexation 15k lignes).

    Le skip reste actif pendant le sync pour bloquer toute cascade post_save
    (PBC / BenefitAttachment) si un signal se déclenche encore pendant les bulk.
    """
    with skip_heavy_opensearch_reindex():
        payroll.save(username=user.login_name)
        sync_payroll_status_to_opensearch(payroll)


def sync_benefit_status_batch_to_opensearch(payroll_id, benefit_ids, status):
    """
    Mise à jour partielle benefit.status dans OpenSearch (fin de job ou par lots).
    Index payroll_benefit_consumption (id = PBC) et benefit_consumption (id = benefit).
    """
    if not _opensearch_enabled() or not benefit_ids:
        return

    try:
        from payroll.documents import (
            BenefitConsumptionDocument,
            PayrollBenefitConsumptionDocument,
        )
        from payroll.models import PayrollBenefitConsumption
    except ImportError:
        return

    benefit_ids = [str(bid) for bid in benefit_ids]
    pbc_rows = PayrollBenefitConsumption.objects.filter(
        payroll_id=payroll_id,
        benefit_id__in=benefit_ids,
        is_deleted=False,
    ).values_list("id", "benefit_id")

    pbc_actions = []
    bc_actions = []
    status_value = str(status)
    benefit_doc_patch = {"status": status_value}
    pbc_doc_patch = {"benefit": benefit_doc_patch}

    pbc_doc = PayrollBenefitConsumptionDocument()
    bc_doc = BenefitConsumptionDocument()
    if pbc_doc.is_sync_disabled() and bc_doc.is_sync_disabled():
        return

    for pbc_id, benefit_id in pbc_rows:
        pbc_actions.append(
            {
                "_op_type": "update",
                "_index": pbc_doc._index._name,
                "_id": str(pbc_id),
                "doc": pbc_doc_patch,
            }
        )
        bc_actions.append(
            {
                "_op_type": "update",
                "_index": bc_doc._index._name,
                "_id": str(benefit_id),
                "doc": benefit_doc_patch,
            }
        )

    client = pbc_doc._get_connection()
    from opensearchpy.helpers import bulk as os_bulk

    for actions, label in ((pbc_actions, "pbc"), (bc_actions, "benefit_consumption")):
        if not actions:
            continue
        offset = 0
        while offset < len(actions):
            chunk = actions[offset : offset + BENEFIT_STATUS_SYNC_CHUNK]
            offset += BENEFIT_STATUS_SYNC_CHUNK
            try:
                success, failed = os_bulk(
                    client, chunk, raise_on_error=False, refresh=False
                )
                failed_count = len(failed) if isinstance(failed, list) else (failed or 0)
                logger.info(
                    "OpenSearch light benefit.status sync payroll_id=%s index=%s ok=%s err=%s",
                    payroll_id,
                    label,
                    success,
                    failed_count,
                )
            except Exception as exc:
                logger.warning(
                    "OpenSearch benefit.status bulk failed payroll_id=%s index=%s: %s",
                    payroll_id,
                    label,
                    exc,
                    exc_info=True,
                )


def _opensearch_enabled():
    return (
        "opensearch_reports" in apps.app_configs
        and not getattr(settings, "IS_UNIT_TEST_ENV", False)
    )


def _payroll_status_doc_patch(payroll):
    return {
        "status": payroll.status,
        "name": payroll.name,
        "payment_method": payroll.payment_method or None,
    }


def sync_payroll_status_to_opensearch(payroll):
    """
    Mise à jour partielle OpenSearch après changement de statut paie seul.

    N'appelle jamais prepare() par bénéficiaire : bulk update sur payroll + PBC.
    """
    if not _opensearch_enabled():
        return

    try:
        from payroll.documents import PayrollBenefitConsumptionDocument, PayrollDocument
    except ImportError:
        logger.debug("OpenSearch payroll documents not loaded; skip light sync.")
        return

    doc_patch = _payroll_status_doc_patch(payroll)
    payroll_doc = PayrollDocument()
    pbc_doc = PayrollBenefitConsumptionDocument()

    if not payroll_doc.is_sync_disabled():
        updated, errors = _bulk_partial_doc_updates(
            payroll_doc._get_connection(),
            payroll_doc._index._name,
            [str(payroll.id)],
            doc_patch,
        )
        logger.info(
            "OpenSearch light payroll index sync payroll_id=%s: updated=%s errors=%s",
            payroll.id,
            updated,
            errors,
        )

    if pbc_doc.is_sync_disabled():
        return

    _bulk_update_nested_payroll_status(payroll, pbc_doc)


def _bulk_partial_doc_updates(client, index_name, doc_ids, doc_patch):
    """Bulk update partiel (pas de prepare / pas de SQL par facture)."""
    from opensearchpy.helpers import bulk as os_bulk

    actions = []
    updated = 0
    errors = 0
    for doc_id in doc_ids:
        actions.append(
            {
                "_op_type": "update",
                "_index": index_name,
                "_id": str(doc_id),
                "doc": doc_patch,
            }
        )
        if len(actions) >= BULK_CHUNK_SIZE:
            ok, err = _flush_bulk(client, os_bulk, actions)
            updated += ok
            errors += err
            actions = []
    if actions:
        ok, err = _flush_bulk(client, os_bulk, actions)
        updated += ok
        errors += err
    return updated, errors


def _bulk_update_nested_payroll_status(payroll, document_cls):
    from opensearchpy.helpers import bulk as os_bulk

    from payroll.models import PayrollBenefitConsumption

    client = document_cls._get_connection()
    index_name = document_cls._index._name
    doc_patch = {"payroll": _payroll_status_doc_patch(payroll)}

    ids_qs = PayrollBenefitConsumption.objects.filter(
        payroll_id=payroll.id,
        is_deleted=False,
    ).values_list("id", flat=True)

    actions = []
    updated = 0
    errors = 0

    for pbc_id in ids_qs.iterator(chunk_size=2000):
        actions.append(
            {
                "_op_type": "update",
                "_index": index_name,
                "_id": str(pbc_id),
                "doc": doc_patch,
            }
        )
        if len(actions) >= BULK_CHUNK_SIZE:
            ok, err = _flush_bulk(client, os_bulk, actions)
            updated += ok
            errors += err
            actions = []

    if actions:
        ok, err = _flush_bulk(client, os_bulk, actions)
        updated += ok
        errors += err

    logger.info(
        "OpenSearch light payroll.status sync for payroll_id=%s index=%s: updated=%s errors=%s",
        payroll.id,
        index_name,
        updated,
        errors,
    )


def _flush_bulk(client, os_bulk, actions):
    try:
        success, failed = os_bulk(client, actions, raise_on_error=False, refresh=False)
        failed_count = len(failed) if isinstance(failed, list) else (failed or 0)
        return success or 0, failed_count
    except Exception as exc:
        logger.warning("OpenSearch bulk payroll.status update failed: %s", exc, exc_info=True)
        return 0, len(actions)


def _bulk_index_instances(document, instances, *, payroll_id, label):
    """Indexe un lot d'instances via prepare() (select_related recommandé sur le queryset)."""
    if document.is_sync_disabled() or not instances:
        return 0, 0
    from opensearchpy.helpers import bulk as os_bulk

    client = document._get_connection()
    actions = []
    for instance in instances:
        if document.should_index_object(instance):
            actions.append(document._prepare_action(instance, "index"))
    if not actions:
        return 0, 0
    ok, err = _flush_bulk(client, os_bulk, actions)
    logger.info(
        "OpenSearch creation index payroll_id=%s index=%s label=%s ok=%s err=%s",
        payroll_id,
        document._index._name,
        label,
        ok,
        err,
    )
    return ok, err


def _bulk_index_queryset(document, queryset, *, payroll_id, label, chunk_size=None):
    chunk_size = chunk_size or BULK_CHUNK_SIZE
    updated = 0
    errors = 0
    chunk = []
    for instance in queryset.iterator(chunk_size=chunk_size):
        chunk.append(instance)
        if len(chunk) >= chunk_size:
            ok, err = _bulk_index_instances(document, chunk, payroll_id=payroll_id, label=label)
            updated += ok
            errors += err
            chunk = []
    if chunk:
        ok, err = _bulk_index_instances(document, chunk, payroll_id=payroll_id, label=label)
        updated += ok
        errors += err
    return updated, errors


def sync_payroll_creation_to_opensearch(payroll):
    """
    Indexation OpenSearch unique en fin de création de paie (après bulk_create DB).

    Remplace la cascade post_save (15k × prepare) déclenchée par payroll.save().
    Utilise PayrollCreationOpenSearchContext pour éviter le N+1 dans prepare().
    """
    if not _opensearch_enabled():
        return

    try:
        from payroll.documents import (
            BenefitAttachmentDocument,
            BenefitConsumptionDocument,
            PayrollBenefitConsumptionDocument,
            PayrollDocument,
        )
        from payroll.models import BenefitAttachment, BenefitConsumption, Payroll
        from payroll.opensearch_creation_context import (
            build_payroll_creation_opensearch_context,
            pbc_queryset_for_creation,
            reset_payroll_creation_opensearch_context,
            use_payroll_creation_opensearch_context,
        )
    except ImportError:
        logger.debug("OpenSearch payroll documents not loaded; skip creation sync.")
        return

    payroll_id = getattr(payroll, "id", payroll)
    payroll = Payroll.objects.select_related(
        "payment_plan",
        "payment_plan__benefit_plan_type",
        "payment_cycle",
    ).get(id=payroll_id)

    creation_ctx = build_payroll_creation_opensearch_context(payroll)
    ctx_token = use_payroll_creation_opensearch_context(creation_ctx)

    totals = {"payroll": 0, "pbc": 0, "benefit_consumption": 0, "benefit_attachment": 0}
    errors = {"payroll": 0, "pbc": 0, "benefit_consumption": 0, "benefit_attachment": 0}

    try:
        payroll_doc = PayrollDocument()
        if not payroll_doc.is_sync_disabled():
            ok, err = _bulk_index_instances(
                payroll_doc, [payroll], payroll_id=payroll_id, label="payroll"
            )
            totals["payroll"] = ok
            errors["payroll"] = err

        pbc_qs = pbc_queryset_for_creation(payroll_id)
        pbc_doc = PayrollBenefitConsumptionDocument()
        ok, err = _bulk_index_queryset(
            pbc_doc, pbc_qs, payroll_id=payroll_id, label="payroll_benefit_consumption"
        )
        totals["pbc"] = ok
        errors["pbc"] = err

        benefit_ids = creation_ctx.benefit_ids
        if benefit_ids:
            bc_doc = BenefitConsumptionDocument()
            bc_qs = BenefitConsumption.objects.filter(
                id__in=benefit_ids,
                is_deleted=False,
            ).select_related("individual", "individual__location")
            ok, err = _bulk_index_queryset(
                bc_doc, bc_qs, payroll_id=payroll_id, label="benefit_consumption"
            )
            totals["benefit_consumption"] = ok
            errors["benefit_consumption"] = err

            ba_doc = BenefitAttachmentDocument()
            ba_qs = BenefitAttachment.objects.filter(
                benefit_id__in=benefit_ids,
                is_deleted=False,
            ).select_related("bill", "benefit", "benefit__individual", "benefit__individual__location")
            ok, err = _bulk_index_queryset(
                ba_doc, ba_qs, payroll_id=payroll_id, label="benefit_attachment"
            )
            totals["benefit_attachment"] = ok
            errors["benefit_attachment"] = err
    finally:
        reset_payroll_creation_opensearch_context(ctx_token)

    logger.info(
        "OpenSearch creation sync completed payroll_id=%s totals=%s errors=%s",
        payroll_id,
        totals,
        errors,
    )
