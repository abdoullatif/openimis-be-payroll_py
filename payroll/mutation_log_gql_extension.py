"""Champs GraphQL pour la barre de tâches sur mutationLogs (core.MutationLog)."""

import graphene

from payroll.mutation_log_task_bar import reconcile_mutation_log_for_task_bar


def _reconciled_for_node(root):
    cached = getattr(root, "_payroll_task_bar_reconciled", None)
    if cached is None:
        cached = reconcile_mutation_log_for_task_bar(root, persist=True)
        root._payroll_task_bar_reconciled = cached
    return cached


def register_mutation_log_task_bar_fields():
    """
    Étend MutationLogGQLType (core) pour la barre de tâches verticale.
    Les champs doivent être ajoutés à _meta.fields pour apparaître dans le schéma Graphene.
    """
    from core.schema import MutationLogGQLType

    if getattr(MutationLogGQLType, "_payroll_task_bar_extended", False):
        return

    MutationLogGQLType._meta.fields["task_bar_status"] = graphene.Field(
        graphene.String,
        description=(
            "Statut barre de tâches : RECEIVED, SUCCESS, ERROR, CANCELLED, STALE."
        ),
    )
    MutationLogGQLType._meta.fields["should_stop_polling"] = graphene.Field(
        graphene.Boolean,
        description="Si true, arrêter le polling mutationLogs pour cette mutation.",
    )
    MutationLogGQLType._meta.fields["task_bar_message"] = graphene.Field(
        graphene.String,
        description="Message optionnel (annulation, timeout, erreur job).",
    )

    @staticmethod
    def resolve_task_bar_status(root, info):
        return _reconciled_for_node(root)["task_bar_status"]

    @staticmethod
    def resolve_should_stop_polling(root, info):
        return _reconciled_for_node(root)["should_stop_polling"]

    @staticmethod
    def resolve_task_bar_message(root, info):
        return _reconciled_for_node(root)["message"]

    MutationLogGQLType.resolve_task_bar_status = resolve_task_bar_status
    MutationLogGQLType.resolve_should_stop_polling = resolve_should_stop_polling
    MutationLogGQLType.resolve_task_bar_message = resolve_task_bar_message
    MutationLogGQLType._payroll_task_bar_extended = True
