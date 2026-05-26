import os

from django.apps import AppConfig

from core.custom_filters import CustomFilterRegistryPoint
from payroll.payments_registry import PaymentsMethodRegistryPoint

MODULE_NAME = 'payroll'


def _reopen_mutation_after_create_payroll(
    sender, mutation_log_id, error_messages=None, **kwargs
):
    """Après mark_as_successful du core : garder RECEIVED pendant l'indexation OS."""
    if getattr(sender, "__name__", None) != "CreatePayrollMutation":
        return
    if error_messages:
        return
    from core.models import MutationLog

    mutation_log = MutationLog.objects.filter(id=mutation_log_id).first()
    if not mutation_log or not mutation_log.client_mutation_id:
        return
    from payroll.opensearch_indexing_progress import reopen_mutation_log_for_opensearch_indexing

    reopen_mutation_log_for_opensearch_indexing(mutation_log.client_mutation_id)

DEFAULT_CONFIG = {
    "gql_payment_point_search_perms": ["201001"],
    "gql_payment_point_create_perms": ["201002"],
    "gql_payment_point_update_perms": ["201003"],
    "gql_payment_point_delete_perms": ["201004"],
    "gql_payroll_search_perms": ["202001"],
    "gql_payroll_create_perms": ["202002"],
    "gql_payroll_delete_perms": ["202004"],
    "gql_csv_reconciliation_search_perms": ["206001"],
    "gql_csv_reconciliation_create_perms": ["206002"],
    "payroll_accept_event": "payroll.accept_payroll",
    "payroll_reconciliation_event": "payroll.payroll_reconciliation",
    "payroll_reject_event": "payroll.payroll_reject",
    "csv_reconciliation_field_mapping": {
        'payrollbenefitconsumption__payroll__name': 'Nom de paie',
        'payrollbenefitconsumption__payroll__status': 'Statut de paie',
        'individual__first_name': 'Prénom',
        'individual__last_name': 'Nom',
        'individual__dob': 'Date de naissance',
        'code': 'Code',
        'status': 'Statut',
        'amount': 'Montant',
        'type': 'Type',
        'receipt': 'Reçu',
    },
    "csv_reconciliation_status_column": "Statut",
    "csv_reconciliation_paid_extra_field": "Payé",
    "csv_reconciliation_receipt_column": "receipt",
    "csv_reconciliation_errors_column": "errors",
    "csv_reconciliation_code_column": "code",
    "csv_reconciliation_paid_yes": "Oui",
    "csv_reconciliation_paid_no": "Non",
    # Additional custom columns (noms internes, utilisés dans services.py)
    "csv_reconciliation_additional_columns": [
        "code_menage",
        "numero_paie",
        "code_empreinte",
    ],
    "payroll_delete_event": "payroll.payroll_delete",
    "benefit_delete_event": "payroll.benefit_delete",

    # Valeurs par défaut depuis .env ; écrasées par ModuleConfiguration (Django Admin) si présentes
    "gateway_base_url": os.getenv("PAYMENT_GATEWAY_BASE_URL", "http://41.175.18.170:8070/api/mobile/v1/"),
    "endpoint_payment": os.getenv("ENDPOINT_PAYMENT", "mock/payment"),
    "endpoint_reconciliation": os.getenv("ENDPOINT_RECONCILIATION", "mock/reconciliation"),
    "payment_gateway_api_key": os.getenv("PAYMENT_GATEWAY_API_KEY"),
    "payment_gateway_basic_auth_username": os.getenv("PAYMENT_GATEWAY_BASIC_AUTH_USERNAME"),
    "payment_gateway_basic_auth_password": os.getenv("PAYMENT_GATEWAY_BASIC_AUTH_PASSWORD"),
    "payment_gateway_timeout": int(os.getenv("PAYMENT_GATEWAY_TIMEOUT", "5")),
    "payment_gateway_auth_type": os.getenv("PAYMENT_GATEWAY_AUTH_TYPE", "basic"),
    "payment_gateway_class": "payroll.payment_gateway.MockedPaymentGatewayConnector",
    "receipt_length": 8,
    "reconciliation_callback_api_key": os.getenv(
        "RECONCILIATION_CALLBACK_API_KEY",
        os.getenv("PAYMENT_GATEWAY_API_KEY"),
    ),
    "reconciliation_callback_username": os.getenv("PAYROLL_RECONCILIATION_CALLBACK_USERNAME", "Admin"),
}


class PayrollConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = MODULE_NAME

    gql_payment_point_search_perms = None
    gql_payment_point_create_perms = None
    gql_payment_point_update_perms = None
    gql_payment_point_delete_perms = None
    gql_payroll_search_perms = None
    gql_payroll_create_perms = None
    gql_payroll_delete_perms = None
    gql_csv_reconciliation_search_perms = None
    gql_csv_reconciliation_create_perms = None
    payroll_accept_event = None
    payroll_reconciliation_event = None
    payroll_reject_event = None
    csv_reconciliation_field_mapping = None
    csv_reconciliation_status_column = None
    csv_reconciliation_paid_extra_field = None
    csv_reconciliation_receipt_column = None
    csv_reconciliation_errors_column = None
    csv_reconciliation_code_column = None
    csv_reconciliation_paid_yes = None
    csv_reconciliation_paid_no = None
    payroll_delete_event = None
    benefit_delete_event = None

    gateway_base_url = None
    endpoint_payment = None
    endpoint_reconciliation = None
    payment_gateway_api_key = None
    payment_gateway_basic_auth_username = None
    payment_gateway_basic_auth_password = None
    payment_gateway_timeout = None
    payment_gateway_auth_type = None
    payment_gateway_class = None
    receipt_length = None
    reconciliation_callback_api_key = None
    reconciliation_callback_username = None

    def ready(self):
        from core.models import ModuleConfiguration
        from core.schema import signal_mutation_module_after_mutating
        from payroll.mutation_log_gql_extension import register_mutation_log_task_bar_fields

        import payroll.tasks  # noqa: F401 — enregistrement tâches Celery

        register_mutation_log_task_bar_fields()
        signal_mutation_module_after_mutating["payroll"].connect(
            _reopen_mutation_after_create_payroll,
            weak=False,
        )
        cfg = ModuleConfiguration.get_or_default(self.name, DEFAULT_CONFIG)
        self.__load_config(cfg)
        self.__register_filters_and_payment_methods()

    @classmethod
    def __load_config(cls, cfg):
        """
        Load all config fields that match current AppConfig class fields, all custom fields have to be loaded separately
        """
        for field in cfg:
            if hasattr(PayrollConfig, field):
                setattr(PayrollConfig, field, cfg[field])

    def __register_filters_and_payment_methods(cls):
        from social_protection.custom_filters import BenefitPlanCustomFilterWizard
        CustomFilterRegistryPoint.register_custom_filters(
            module_name=cls.name,
            custom_filter_class_list=[BenefitPlanCustomFilterWizard]
        )

        from payroll.strategies import (
            StrategyOfflinePayment,
            StrategyOnlinePayment
        )
        PaymentsMethodRegistryPoint.register_payment_method(
            payment_method_class_list=[
                StrategyOfflinePayment(),
                StrategyOnlinePayment(),
            ]
        )

    @staticmethod
    def get_payroll_payment_file_path(payroll_id, file_name=None):
        if file_name:
            return f"csv_reconciliation/payroll_{payroll_id}/{file_name}"
        return f"csv_reconciliation/payroll_{payroll_id}"

    @staticmethod
    def get_payroll_report_file_path(payroll_id, file_name=None):
        if file_name:
            return f"payment_reports/payroll_{payroll_id}/{file_name}"
        return f"payment_reports/payroll_{payroll_id}"
