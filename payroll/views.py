import logging

from django.db import transaction
from rest_framework import views
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response

from core.utils import DefaultStorageFileHandler
from core.views import check_user_rights
from payroll.apps import PayrollConfig
from payroll.models import Payroll, CsvReconciliationUpload, PaymentReport
from payroll.payments_registry import PaymentMethodStorage
from payroll.permissions import ReconciliationCallbackAPIKeyPermission
from payroll.reconciliation_callback_service import ReconciliationCallbackService
from payroll.services import CsvReconciliationService

logger = logging.getLogger(__name__)


@api_view(["POST"])
@permission_classes([check_user_rights(PayrollConfig.gql_payroll_create_perms, )])
def send_callback_to_openimis(request):
    try:
        user = request.user
        payroll_id, response_from_gateway, rejected_bills = \
            _resolve_send_callback_to_imis_args(request)
        payroll = Payroll.objects.get(id=payroll_id)
        strategy = PaymentMethodStorage.get_chosen_payment_method(payroll.payment_method)
        if strategy:
            # save the reponse from gateway in openIMIS
            strategy.acknowledge_of_reponse_view(
                payroll,
                response_from_gateway,
                user,
                rejected_bills
            )
        return Response({'success': True, 'error': None}, status=201)
    except ValueError as exc:
        logger.error("Error while sending callback to openIMIS", exc_info=exc)
        return Response({'success': False, 'error': str(exc)}, status=400)
    except Exception as exc:
        logger.error("Unexpected error while sending callback to openIMIS", exc_info=exc)
        return Response({'success': False, 'error': str(exc)}, status=500)


def _resolve_send_callback_to_imis_args(request):
    payroll_id = request.data.get('payroll_id')
    response_from_gateway = request.data.get('response_from_gateway')
    rejected_bills = request.data.get('rejected_bills')
    if not payroll_id:
        raise ValueError('Payroll Id not provided')
    if not response_from_gateway:
        raise ValueError('Response from gateway not provided')
    if rejected_bills is None:
        raise ValueError('Rejected Bills not provided')

    return payroll_id, response_from_gateway, rejected_bills


@api_view(["POST"])
@authentication_classes([])
@permission_classes([ReconciliationCallbackAPIKeyPermission])
def reconciliation_callback_from_operator(request):
    """
    Callback push opérateur : identification (comme pull) + success + receipt/transactionId si succès.
  """
    try:
        user = ReconciliationCallbackService.get_callback_user()
        result = ReconciliationCallbackService(user).process(request.data)
        return Response(result, status=201)
    except ValueError as exc:
        logger.warning("Reconciliation callback validation error: %s", exc)
        return Response({"success": False, "error": str(exc)}, status=400)
    except Exception as exc:
        logger.error("Unexpected error in reconciliation callback", exc_info=exc)
        return Response({"success": False, "error": str(exc)}, status=500)


class CSVReconciliationAPIView(views.APIView):
    permission_classes = [check_user_rights(PayrollConfig.gql_csv_reconciliation_create_perms, )]

    def get(self, request):
        try:
            payroll_id = request.GET.get('payroll_id')
            get_blank = request.GET.get('blank')
            get_blank_bool = get_blank.lower() == 'true'
            file_format = request.GET.get('format', 'csv').lower()

            if get_blank_bool:
                service = CsvReconciliationService(request.user)
                # Excel désactivé: toujours retourner CSV
                in_memory_file = service.download_reconciliation(payroll_id)
                from django.http import HttpResponse
                resp = HttpResponse(
                    in_memory_file.getvalue(),
                    content_type='text/csv'
                )
                resp['Content-Disposition'] = 'attachment; filename="reconciliation.csv"'
                return resp
            else:
                file_name = request.GET.get('payroll_file_name')
                path = PayrollConfig.get_payroll_payment_file_path(payroll_id, file_name)
                file_handler = DefaultStorageFileHandler(path)
                return file_handler.get_file_response_csv(file_name)
        except ValueError as exc:
            logger.error("Error while generating CSV reconciliation", exc_info=exc)
            return Response({'success': False, 'error': str(exc)}, status=400)
        except FileNotFoundError as exc:
            logger.error("File not found", exc_info=exc)
            return Response({'success': False, 'error': str(exc)}, status=404)
        except Exception as exc:
            logger.error("Error while generating CSV reconciliation", exc_info=exc)
            return Response({'success': False, 'error': str(exc)}, status=500)

    @transaction.atomic
    def post(self, request):
        upload = CsvReconciliationUpload()
        payroll_id = request.GET.get('payroll_id')
        try:
            upload.save(username=request.user.login_name)
            file = request.FILES.get('file')
            if not file:
                return Response({'success': False, 'error': 'File is required'}, status=400)
            
            # Valider le type de fichier (CSV uniquement)
            file_name_lower = file.name.lower()
            if not file_name_lower.endswith('.csv'):
                return Response({'success': False, 'error': 'Only CSV files are allowed'}, status=400)
            
            target_file_path = PayrollConfig.get_payroll_payment_file_path(payroll_id, file.name)
            upload.file_name = file.name
            file_handler = DefaultStorageFileHandler(target_file_path)
            file_handler.check_file_path()
            service = CsvReconciliationService(request.user)
            file_to_upload, errors, summary = service.upload_reconciliation(payroll_id, file, upload)
            if errors:
                upload.status = CsvReconciliationUpload.Status.PARTIAL_SUCCESS
                upload.error = errors
                upload.json_ext = {'extra_info': summary}
            else:
                upload.status = CsvReconciliationUpload.Status.SUCCESS
                upload.json_ext = {'extra_info': summary}
            upload.save(username=request.user.login_name)
            file_handler.save_file(file_to_upload)
            return Response({'success': True, 'error': None}, status=201)
        except Exception as exc:
            logger.error("Error while uploading CSV reconciliation", exc_info=exc)
            if upload:
                upload.error = {'error': str(exc)}
                upload.payroll = Payroll.objects.filter(id=payroll_id).first()
                upload.status = CsvReconciliationUpload.Status.FAIL
                summary = {
                    'affected_rows': 0,
                }
                upload.json_ext = {'extra_info': summary}
                upload.save(username=request.user.login_name)
            return Response({'success': False, 'error': str(exc)}, status=500)


class PaymentReportAPIView(views.APIView):
    permission_classes = [check_user_rights(PayrollConfig.gql_payroll_create_perms, )]

    def get(self, request):
        try:
            payroll_id = request.GET.get('payroll_id')
            file_name = request.GET.get('file_name')
            if file_name:
                path = PayrollConfig.get_payroll_report_file_path(payroll_id, file_name)
                # Lire le fichier depuis le storage par défaut et le retourner en binaire (PDF)
                from django.core.files.storage import default_storage
                from django.http import FileResponse
                if not default_storage.exists(path):
                    return Response({'success': False, 'error': 'Report file not found'}, status=404)
                file_obj = default_storage.open(path, 'rb')
                response = FileResponse(file_obj, content_type='application/pdf')
                response['Content-Disposition'] = f'attachment; filename="{file_name}"'
                return response
            reports = PaymentReport.objects.filter(payroll_id=payroll_id, is_deleted=False).values('file_name', 'date_created', 'uploaded_by_id')
            return Response({'success': True, 'data': list(reports)}, status=200)
        except FileNotFoundError as exc:
            logger.error("Report file not found", exc_info=exc)
            return Response({'success': False, 'error': str(exc)}, status=404)
        except Exception as exc:
            logger.error("Error while handling payment report GET", exc_info=exc)
            return Response({'success': False, 'error': str(exc)}, status=500)

    @transaction.atomic
    def post(self, request):
        try:
            payroll_id = request.GET.get('payroll_id')
            if not payroll_id:
                return Response({'success': False, 'error': 'payroll_id is required'}, status=400)
            
            file = request.FILES.get('file')
            if not file:
                return Response({'success': False, 'error': 'PDF file is required'}, status=400)
            
            if not str(file.name).lower().endswith('.pdf'):
                return Response({'success': False, 'error': 'Only PDF files are allowed'}, status=400)
            
            # Vérifier que le payroll existe
            payroll = Payroll.objects.filter(id=payroll_id, is_deleted=False).first()
            if not payroll:
                return Response({'success': False, 'error': 'Payroll not found'}, status=404)
            
            target_file_path = PayrollConfig.get_payroll_report_file_path(payroll_id, file.name)
            file_handler = DefaultStorageFileHandler(target_file_path)
            file_handler.check_file_path()
            
            report = PaymentReport(payroll_id=payroll_id, uploaded_by=request.user, file_name=file.name)
            report.save(username=request.user.login_name)
            file_handler.save_file(file)
            
            return Response({'success': True, 'error': None}, status=201)
        except ValueError as exc:
            logger.error("Validation error while uploading payment report", exc_info=exc)
            return Response({'success': False, 'error': str(exc)}, status=400)
        except Exception as exc:
            logger.error("Error while uploading payment report", exc_info=exc)
            return Response({'success': False, 'error': str(exc)}, status=500)
