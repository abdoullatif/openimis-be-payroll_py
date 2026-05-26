from django.apps import apps
from django.conf import settings

is_unit_test_env = getattr(settings, 'IS_UNIT_TEST_ENV', False)

# Check if the 'opensearch_reports' app is in INSTALLED_APPS
if 'opensearch_reports' in apps.app_configs and not is_unit_test_env:
    from opensearch_reports.service import BaseSyncDocument
    from django_opensearch_dsl import fields as opensearch_fields
    from django_opensearch_dsl.registries import registry
    from payroll.models import (
        Payroll,
        PayrollBenefitConsumption,
        BenefitConsumption,
        BenefitAttachment
    )
    from payment_cycle.models import PaymentCycle
    from contribution_plan.models import PaymentPlan
    from individual.models import Individual
    from invoice.models import Bill
    from social_protection.models import Beneficiary, BenefitPlan, BeneficiaryStatus
    from payroll.opensearch_creation_context import (
        benefit_plan_for_individual,
        get_payroll_creation_opensearch_context,
        payment_cycle_fields_from_creation_context,
    )

    class PayrollSkipAwareDocument(BaseSyncDocument):
        """Ignore les saves en masse (tâche Celery paiement / flags json_ext)."""

        def update(self, thing, action, *args, refresh=None, using=None, **kwargs):
            from payroll.opensearch_payroll_status_sync import (
                should_skip_heavy_opensearch_reindex,
            )

            if should_skip_heavy_opensearch_reindex():
                return None
            return super().update(thing, action, *args, refresh=refresh, using=using, **kwargs)

    @registry.register_document
    class PayrollDocument(PayrollSkipAwareDocument):
        DASHBOARD_NAME = 'Payment'

        name = opensearch_fields.KeywordField()
        status = opensearch_fields.KeywordField()
        payment_method = opensearch_fields.KeywordField()
        date_created = opensearch_fields.DateField()
        payment_plan = opensearch_fields.ObjectField(properties={
            'code': opensearch_fields.KeywordField(),
            'name': opensearch_fields.KeywordField(),
        })
        payment_cycle = opensearch_fields.ObjectField(properties={
            'code': opensearch_fields.KeywordField(),
            'status': opensearch_fields.KeywordField(),
            'start_date': opensearch_fields.DateField(),
            'end_date': opensearch_fields.DateField(),
        })
        benefit_plan = opensearch_fields.ObjectField(properties={
            'id': opensearch_fields.KeywordField(),
            'code': opensearch_fields.KeywordField(),
            'name': opensearch_fields.KeywordField(),
        })

        class Index:
            name = 'payroll'
            settings = {
                'number_of_shards': 1,
                'number_of_replicas': 0
            }
            auto_refresh = True

        class Django:
            model = Payroll
            fields = [
                'id'
            ]
            related_models = [PaymentPlan, PaymentCycle]
            queryset_pagination = 5000

        def prepare(self, instance):
            """Préparer les données avec benefit_plan"""
            data = super().prepare(instance)
            
            # Ajouter benefit_plan depuis PaymentPlan
            if instance.payment_plan and instance.payment_plan.benefit_plan:
                bp = instance.payment_plan.benefit_plan
                data['benefit_plan'] = {
                    'id': str(bp.id) if hasattr(bp, 'id') else None,
                    'code': getattr(bp, 'code', None),
                    'name': getattr(bp, 'name', None) or str(bp),
                }
            
            return data

        def get_instances_from_related(self, related_instance):
            if isinstance(related_instance, PaymentPlan):
                return Payroll.objects.filter(payment_plan=related_instance)
            elif isinstance(related_instance, PaymentCycle):
                return Payroll.objects.filter(payment_cycle=related_instance)


    @registry.register_document
    class BenefitConsumptionDocument(PayrollSkipAwareDocument):
        DASHBOARD_NAME = 'Payment'

        photo = opensearch_fields.KeywordField()
        code = opensearch_fields.KeywordField()
        date_due = opensearch_fields.DateField()
        date_created = opensearch_fields.DateField()
        receipt = opensearch_fields.KeywordField()
        amount = opensearch_fields.KeywordField()
        type = opensearch_fields.KeywordField()
        status = opensearch_fields.KeywordField()
        json_ext = opensearch_fields.ObjectField()
        individual = opensearch_fields.ObjectField(properties={
            'id': opensearch_fields.KeywordField(),
            'first_name': opensearch_fields.KeywordField(),
            'last_name': opensearch_fields.KeywordField(),
            'dob': opensearch_fields.DateField(),
            'gender': opensearch_fields.KeywordField(),
        })
        benefit_plan = opensearch_fields.ObjectField(properties={
            'id': opensearch_fields.KeywordField(),
            'code': opensearch_fields.KeywordField(),
            'name': opensearch_fields.KeywordField(),
        })
        # Champs calculés pour analytics
        is_monetary_transfer = opensearch_fields.BooleanField()
        payment_cycle_code = opensearch_fields.KeywordField()
        payment_cycle_start_date = opensearch_fields.DateField()
        payment_cycle_end_date = opensearch_fields.DateField()

        class Index:
            name = 'benefit_consumption'
            settings = {
                'number_of_shards': 1,
                'number_of_replicas': 0
            }
            auto_refresh = True

        class Django:
            model = BenefitConsumption
            related_models = [Individual, BenefitPlan]
            fields = [
                'id'
            ]
            queryset_pagination = 5000

        def prepare(self, instance):
            """Préparer les données pour l'indexation avec les champs calculés"""
            data = super().prepare(instance)
            creation_ctx = get_payroll_creation_opensearch_context()
            payroll_bc = None
            if not creation_ctx:
                try:
                    payroll_bc = (
                        instance.payrollbenefitconsumption_set.select_related(
                            "payroll",
                            "payroll__payment_cycle",
                            "payroll__payment_plan",
                        ).first()
                    )
                except Exception:
                    payroll_bc = None

            if instance.individual:
                individual_json = instance.individual.json_ext or {}
                data['individual']['gender'] = individual_json.get('gender') or instance.individual.gender if hasattr(instance.individual, 'gender') else None
                data['individual']['id'] = str(getattr(instance.individual, 'id', None))

            data['benefit_plan'] = benefit_plan_for_individual(instance.individual)
            if not data['benefit_plan'] and payroll_bc and payroll_bc.payroll and payroll_bc.payroll.payment_plan:
                payment_plan = payroll_bc.payroll.payment_plan
                if payment_plan and payment_plan.benefit_plan:
                    bp = payment_plan.benefit_plan
                    data['benefit_plan'] = {
                        'id': str(bp.id) if hasattr(bp, 'id') else None,
                        'code': getattr(bp, 'code', None),
                        'name': getattr(bp, 'name', None) or str(bp),
                    }

            data['is_monetary_transfer'] = (
                instance.type and 'monetary' in str(instance.type).lower()
            ) or (instance.amount and float(instance.amount) > 0)

            cycle_fields = payment_cycle_fields_from_creation_context()
            if cycle_fields:
                data.update(cycle_fields)
            elif payroll_bc and payroll_bc.payroll and payroll_bc.payroll.payment_cycle:
                cycle = payroll_bc.payroll.payment_cycle
                data['payment_cycle_code'] = getattr(cycle, 'code', None)
                data['payment_cycle_start_date'] = getattr(cycle, 'start_date', None)
                data['payment_cycle_end_date'] = getattr(cycle, 'end_date', None)

            return data

        def get_instances_from_related(self, related_instance):
            if isinstance(related_instance, Individual):
                return BenefitConsumption.objects.filter(individual=related_instance)
            elif isinstance(related_instance, BenefitPlan):
                # Via les données RÉELLES : Beneficiary -> Individual -> BenefitConsumption
                beneficiaries = Beneficiary.objects.filter(
                    benefit_plan=related_instance,
                    is_deleted=False
                )
                individuals = Individual.objects.filter(
                    id__in=beneficiaries.values_list('individual_id', flat=True)
                )
                return BenefitConsumption.objects.filter(individual__in=individuals)


    @registry.register_document
    class PayrollBenefitConsumptionDocument(PayrollSkipAwareDocument):
        DASHBOARD_NAME = 'Payment'

        payroll = opensearch_fields.ObjectField(properties={
            'name': opensearch_fields.KeywordField(),
            'status': opensearch_fields.KeywordField(),
            'payment_method': opensearch_fields.KeywordField(),
            'date_created': opensearch_fields.DateField(),
            'payment_plan': opensearch_fields.ObjectField(properties={
                'code': opensearch_fields.KeywordField(),
                'name': opensearch_fields.KeywordField(),
            }),
            'payment_cycle': opensearch_fields.ObjectField(properties={
                'code': opensearch_fields.KeywordField(),
                'status': opensearch_fields.KeywordField(),
                'start_date': opensearch_fields.DateField(),
                'end_date': opensearch_fields.DateField(),
            })
        })
        benefit = opensearch_fields.ObjectField(properties={
            'code': opensearch_fields.KeywordField(),
            'status': opensearch_fields.KeywordField(),
            'type': opensearch_fields.KeywordField(),
            'receipt': opensearch_fields.KeywordField(),
            'amount': opensearch_fields.FloatField(),
            'photo': opensearch_fields.KeywordField(),
            'date_due': opensearch_fields.DateField(),
            'individual': opensearch_fields.ObjectField(properties={
              'id': opensearch_fields.KeywordField(),
              'first_name': opensearch_fields.KeywordField(),
              'last_name': opensearch_fields.KeywordField(),
              'dob': opensearch_fields.DateField(),
              'gender': opensearch_fields.KeywordField(),
            }),
            'benefit_plan': opensearch_fields.ObjectField(properties={
                'id': opensearch_fields.KeywordField(),
                'code': opensearch_fields.KeywordField(),
                'name': opensearch_fields.KeywordField(),
            }),
            'is_monetary_transfer': opensearch_fields.BooleanField(),
            'payment_cycle_code': opensearch_fields.KeywordField(),
            'payment_cycle_start_date': opensearch_fields.DateField(),
            'payment_cycle_end_date': opensearch_fields.DateField()
        })
        location = opensearch_fields.ObjectField(properties={
            'code': opensearch_fields.KeywordField(),
            'name': opensearch_fields.KeywordField(),
            'type': opensearch_fields.KeywordField(),
            'region': opensearch_fields.KeywordField(),
            'prefecture': opensearch_fields.KeywordField(),
            'sous_prefecture': opensearch_fields.KeywordField(),
            'district': opensearch_fields.KeywordField(),
        })

        class Index:
            name = 'payroll_benefit_consumption'
            settings = {
                'number_of_shards': 1,
                'number_of_replicas': 0
            }
            auto_refresh = True

        class Django:
            model = PayrollBenefitConsumption
            related_models = [Payroll, BenefitConsumption]
            fields = [
                'id'
            ]
            queryset_pagination = 5000

        def prepare(self, instance):
            """Préparer les données avec les champs calculés"""
            data = super().prepare(instance)
            
            # Ajouter le genre dans benefit.individual
            if instance.benefit and instance.benefit.individual:
                individual_json = instance.benefit.individual.json_ext or {}
                if 'individual' in data.get('benefit', {}):
                    data['benefit']['individual']['gender'] = (
                        individual_json.get('gender') or 
                        getattr(instance.benefit.individual, 'gender', None)
                    )
                    data['benefit']['individual']['id'] = str(getattr(instance.benefit.individual, 'id', None))
                
                plan = benefit_plan_for_individual(instance.benefit.individual)
                if plan:
                    data['benefit']['benefit_plan'] = plan
                elif instance.payroll and instance.payroll.payment_plan:
                    payment_plan = instance.payroll.payment_plan
                    if payment_plan and payment_plan.benefit_plan:
                        bp = payment_plan.benefit_plan
                        data['benefit']['benefit_plan'] = {
                            'id': str(bp.id) if hasattr(bp, 'id') else None,
                            'code': getattr(bp, 'code', None),
                            'name': getattr(bp, 'name', None) or str(bp),
                        }

            # Ajouter is_monetary_transfer
            if instance.benefit:
                data['benefit']['is_monetary_transfer'] = (
                    instance.benefit.type and 'monetary' in str(instance.benefit.type).lower()
                ) or (instance.benefit.amount and float(instance.benefit.amount) > 0)
            
            # Ajouter payment_cycle_code
            if instance.payroll and instance.payroll.payment_cycle:
                cycle = instance.payroll.payment_cycle
                data['benefit']['payment_cycle_code'] = getattr(cycle, 'code', None)
            
            return data

        def prepare_location(self, instance):
            individual = getattr(instance.benefit, 'individual', None)
            creation_ctx = get_payroll_creation_opensearch_context()
            if creation_ctx and individual:
                return creation_ctx.location_by_individual.get(str(individual.id))

            location = getattr(individual, 'location', None)
            if not location and individual:
                group_rel = individual.groupindividuals.select_related('group__location').first()
                if group_rel and group_rel.group and group_rel.group.location:
                    location = group_rel.group.location

            if not location and individual and getattr(individual, 'json_ext', None):
                json_ext = individual.json_ext or {}
                return {
                    'region': json_ext.get('region'),
                    'prefecture': json_ext.get('prefecture'),
                    'sous_prefecture': json_ext.get('sous_prefecture'),
                    'district': json_ext.get('district'),
                }

            if not location:
                return None

            data = {
                'code': location.code,
                'name': location.name,
                'type': location.type,
            }

            current = location
            while current:
                loc_type = getattr(current, 'type', None)
                if loc_type == 'R':
                    data.setdefault('region', current.name)
                    data.setdefault('prefecture', current.name)
                elif loc_type == 'P':
                    data.setdefault('prefecture', current.name)
                elif loc_type == 'S':
                    data.setdefault('sous_prefecture', current.name)
                elif loc_type == 'D':
                    data.setdefault('district', current.name)
                elif loc_type == 'W':
                    data.setdefault('sous_prefecture', current.name)
                current = current.parent

            return data

        def get_instances_from_related(self, related_instance):
            if isinstance(related_instance, Payroll):
                from payroll.opensearch_payroll_status_sync import (
                    should_skip_payroll_related_reindex,
                )

                if should_skip_payroll_related_reindex():
                    return PayrollBenefitConsumption.objects.none()
                return PayrollBenefitConsumption.objects.filter(payroll=related_instance)
            elif isinstance(related_instance, BenefitConsumption):
                from payroll.opensearch_payroll_status_sync import (
                    should_skip_heavy_opensearch_reindex,
                )

                if should_skip_heavy_opensearch_reindex():
                    return PayrollBenefitConsumption.objects.none()
                return PayrollBenefitConsumption.objects.filter(benefit=related_instance)


    @registry.register_document
    class BenefitAttachmentDocument(PayrollSkipAwareDocument):
        DASHBOARD_NAME = 'Invoice'

        bill = opensearch_fields.ObjectField(properties={
            'code': opensearch_fields.KeywordField(),
            'code_ext': opensearch_fields.KeywordField(),
            'code_tp': opensearch_fields.KeywordField(),
            'status': opensearch_fields.KeywordField(),
            'currency_code': opensearch_fields.KeywordField(),
            'note': opensearch_fields.KeywordField(),
            'terms': opensearch_fields.KeywordField(),
            'date_created': opensearch_fields.DateField(),
            'date_due': opensearch_fields.DateField(),
            'date_payed': opensearch_fields.DateField(),
            'amount_total': opensearch_fields.FloatField(),
        })
        benefit = opensearch_fields.ObjectField(properties={
            'code': opensearch_fields.KeywordField(),
            'status': opensearch_fields.KeywordField(),
            'type': opensearch_fields.KeywordField(),
            'receipt': opensearch_fields.KeywordField(),
            'amount': opensearch_fields.KeywordField(),
            'photo': opensearch_fields.KeywordField(),
            'date_due': opensearch_fields.DateField(),
            'individual': opensearch_fields.ObjectField(properties={
                'first_name': opensearch_fields.KeywordField(),
                'last_name': opensearch_fields.KeywordField(),
                'dob': opensearch_fields.DateField(),
                'gender': opensearch_fields.KeywordField(),
            }),
            'benefit_plan': opensearch_fields.ObjectField(properties={
                'id': opensearch_fields.KeywordField(),
                'code': opensearch_fields.KeywordField(),
                'name': opensearch_fields.KeywordField(),
            }),
            'is_monetary_transfer': opensearch_fields.BooleanField(),
            'payment_cycle_code': opensearch_fields.KeywordField()
        })
        location = opensearch_fields.ObjectField(properties={
            'code': opensearch_fields.KeywordField(),
            'name': opensearch_fields.KeywordField(),
            'type': opensearch_fields.KeywordField(),
            'region': opensearch_fields.KeywordField(),
            'prefecture': opensearch_fields.KeywordField(),
            'sous_prefecture': opensearch_fields.KeywordField(),
            'district': opensearch_fields.KeywordField(),
        })
        payment_plan_codes = opensearch_fields.KeywordField()
        payment_cycle_codes = opensearch_fields.KeywordField()
        payroll_names = opensearch_fields.KeywordField()
        payroll = opensearch_fields.NestedField(properties={
            'name': opensearch_fields.KeywordField(),
            'status': opensearch_fields.KeywordField(),
            'payment_method': opensearch_fields.KeywordField(),
            'date_created': opensearch_fields.DateField(),
            'payment_plan': opensearch_fields.ObjectField(properties={
                'code': opensearch_fields.KeywordField(),
                'name': opensearch_fields.KeywordField(),
            }),
            'payment_cycle': opensearch_fields.ObjectField(properties={
                'code': opensearch_fields.KeywordField(),
                'status': opensearch_fields.KeywordField(),
                'start_date': opensearch_fields.DateField(),
                'end_date': opensearch_fields.DateField(),
            })
        })

        class Index:
            name = 'benefit_attachment'
            settings = {
                'number_of_shards': 1,
                'number_of_replicas': 0
            }
            auto_refresh = True

        class Django:
            model = BenefitAttachment
            related_models = [Payroll, Bill, BenefitConsumption]
            fields = [
                'id'
            ]
            queryset_pagination = 5000

        def get_instances_from_related(self, related_instance):
            if isinstance(related_instance, Payroll):
                from payroll.opensearch_payroll_status_sync import (
                    should_skip_payroll_related_reindex,
                )

                if should_skip_payroll_related_reindex():
                    return BenefitAttachment.objects.none()
                return BenefitAttachment.objects.filter(
                    benefit__payrollbenefitconsumption__payroll=related_instance
                )
            elif isinstance(related_instance, Bill):
                return BenefitAttachment.objects.filter(bill=related_instance)
            elif isinstance(related_instance, BenefitConsumption):
                from payroll.opensearch_payroll_status_sync import (
                    should_skip_heavy_opensearch_reindex,
                )

                if should_skip_heavy_opensearch_reindex():
                    return BenefitAttachment.objects.none()
                return BenefitAttachment.objects.filter(benefit=related_instance)

        def prepare(self, instance):
            data = super().prepare(instance)

            if instance.benefit and instance.benefit.individual:
                individual_json = instance.benefit.individual.json_ext or {}
                if 'individual' in data.get('benefit', {}):
                    data['benefit']['individual']['gender'] = (
                        individual_json.get('gender') or
                        getattr(instance.benefit.individual, 'gender', None)
                    )

                plan = benefit_plan_for_individual(instance.benefit.individual)
                if plan:
                    data['benefit']['benefit_plan'] = plan
                elif not get_payroll_creation_opensearch_context():
                    pbc = PayrollBenefitConsumption.objects.filter(
                        benefit=instance.benefit
                    ).select_related(
                        'payroll__payment_plan__benefit_plan_type',
                        'payroll__payment_cycle',
                    ).first()
                    if pbc and pbc.payroll and pbc.payroll.payment_plan and pbc.payroll.payment_plan.benefit_plan:
                        bp = pbc.payroll.payment_plan.benefit_plan
                        data['benefit']['benefit_plan'] = {
                            'id': str(bp.id) if hasattr(bp, 'id') else None,
                            'code': getattr(bp, 'code', None),
                            'name': getattr(bp, 'name', None) or str(bp),
                        }

                creation_ctx = get_payroll_creation_opensearch_context()
                if creation_ctx and creation_ctx.payroll.payment_cycle:
                    data['benefit']['payment_cycle_code'] = getattr(
                        creation_ctx.payroll.payment_cycle, 'code', None
                    )
                elif not creation_ctx:
                    pbc = PayrollBenefitConsumption.objects.filter(
                        benefit=instance.benefit
                    ).select_related('payroll__payment_cycle').first()
                    if pbc and pbc.payroll and pbc.payroll.payment_cycle:
                        data['benefit']['payment_cycle_code'] = getattr(
                            pbc.payroll.payment_cycle, 'code', None
                        )

            return data

        def prepare_location(self, instance):
            individual = getattr(instance.benefit, 'individual', None)
            creation_ctx = get_payroll_creation_opensearch_context()
            if creation_ctx and individual:
                return creation_ctx.location_by_individual.get(str(individual.id))

            location = getattr(individual, 'location', None)
            if not location and individual and getattr(individual, 'json_ext', None):
                json_ext = individual.json_ext or {}
                return {
                    'region': json_ext.get('region'),
                    'prefecture': json_ext.get('prefecture'),
                    'sous_prefecture': json_ext.get('sous_prefecture'),
                    'district': json_ext.get('district'),
                }

            if not location:
                return None

            data = {
                'code': location.code,
                'name': location.name,
                'type': location.type,
            }

            current = location
            while current:
                loc_type = getattr(current, 'type', None)
                if loc_type == 'R':
                    data.setdefault('region', current.name)
                    data.setdefault('prefecture', current.name)
                elif loc_type == 'P':
                    data.setdefault('prefecture', current.name)
                elif loc_type == 'S':
                    data.setdefault('sous_prefecture', current.name)
                elif loc_type == 'D':
                    data.setdefault('district', current.name)
                elif loc_type == 'W':
                    data.setdefault('sous_prefecture', current.name)
                current = current.parent

            return data

        def prepare_payroll(self, instance):
            creation_ctx = get_payroll_creation_opensearch_context()
            if creation_ctx:
                return creation_ctx.payroll_nested_doc
            return []

        def prepare_payment_plan_codes(self, instance):
            creation_ctx = get_payroll_creation_opensearch_context()
            if creation_ctx:
                return creation_ctx.payment_plan_codes

            payrolls = PayrollBenefitConsumption.objects.filter(
                benefit=instance.benefit
            ).select_related("payroll", "payroll__payment_plan")

            codes = []
            for item in payrolls:
                payment_plan = getattr(item.payroll, "payment_plan", None)
                if payment_plan and payment_plan.code:
                    codes.append(payment_plan.code)
            return codes

        def prepare_payment_cycle_codes(self, instance):
            creation_ctx = get_payroll_creation_opensearch_context()
            if creation_ctx:
                return creation_ctx.payment_cycle_codes

            payrolls = PayrollBenefitConsumption.objects.filter(
                benefit=instance.benefit
            ).select_related("payroll", "payroll__payment_cycle")

            codes = []
            for item in payrolls:
                payment_cycle = getattr(item.payroll, "payment_cycle", None)
                if payment_cycle and payment_cycle.code:
                    codes.append(payment_cycle.code)
            return codes

        def prepare_payroll_names(self, instance):
            creation_ctx = get_payroll_creation_opensearch_context()
            if creation_ctx:
                return creation_ctx.payroll_names

            payrolls = PayrollBenefitConsumption.objects.filter(
                benefit=instance.benefit
            ).select_related("payroll")

            names = []
            for item in payrolls:
                payroll = getattr(item, "payroll", None)
                if payroll and payroll.name:
                    names.append(payroll.name)
            return names
