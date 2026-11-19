import importlib

from payroll.apps import PayrollConfig


class PaymentGatewayConfig:
    def __init__(self):
        self.gateway_base_url = PayrollConfig.gateway_base_url
        self.endpoint_payment = PayrollConfig.endpoint_payment
        self.endpoint_reconciliation = PayrollConfig.endpoint_reconciliation
        self.endpoint_beneficiary = PayrollConfig.endpoint_beneficiary
        self.api_key = PayrollConfig.payment_gateway_api_key
        self.basic_auth_username = PayrollConfig.payment_gateway_basic_auth_username
        self.basic_auth_password = PayrollConfig.payment_gateway_basic_auth_password
        self.timeout = PayrollConfig.payment_gateway_timeout
        self.auth_type = PayrollConfig.payment_gateway_auth_type

    def get_headers(self):
        if self.auth_type == 'token':
            return {
                'Authorization': f'Bearer {self.api_key}',
                'Content-Type': 'application/json',
            }
        elif self.auth_type == 'basic':
            import base64
            auth_str = f"{self.basic_auth_username}:{self.basic_auth_password}"
            auth_bytes = auth_str.encode('utf-8')
            auth_base64 = base64.b64encode(auth_bytes).decode('utf-8')
            return {
                'Authorization': f'Basic {auth_base64}',
                'Content-Type': 'application/json',
            }
        else:
            return {
                'Content-Type': 'application/json',
            }

    def get_payment_gateway_connector(self):
        module_name, class_name = PayrollConfig.payment_gateway_class.rsplit('.', 1)
        module = importlib.import_module(module_name)
        return getattr(module, class_name)

    def get_payment_endpoint(self):
        return self.endpoint_payment

    def get_reconciliation_endpoint(self):
        return self.endpoint_reconciliation

    def get_beneficiary_endpoint(self):
        return self.endpoint_beneficiary