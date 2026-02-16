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

    @registry.register_document
    class PayrollDocument(BaseSyncDocument):
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
    class BenefitConsumptionDocument(BaseSyncDocument):
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
            # Pré-calcule le lien vers le payroll si existant
            payroll_bc = None
            try:
                payroll_bc = instance.payrollbenefitconsumption_set.first()
            except Exception:
                payroll_bc = None
            
            # Récupérer le genre de l'individu
            if instance.individual:
                # Essayer de récupérer le genre depuis json_ext ou directement
                individual_json = instance.individual.json_ext or {}
                data['individual']['gender'] = individual_json.get('gender') or instance.individual.gender if hasattr(instance.individual, 'gender') else None
                data['individual']['id'] = str(getattr(instance.individual, 'id', None))
            
            # Récupérer le benefit_plan depuis les données RÉELLES : Individual -> Beneficiary -> BenefitPlan
            benefit_plan_data = None
            if instance.individual:
                # Récupérer le Beneficiary actif de l'individu au moment de la consommation
                # On prend le Beneficiary actif le plus récent ou celui qui correspond à la date de consommation
                beneficiary = Beneficiary.objects.filter(
                    individual=instance.individual,
                    status=BeneficiaryStatus.ACTIVE,
                    is_deleted=False
                ).order_by('-date_valid_from').first()
                
                # Si pas de bénéficiaire actif, prendre le plus récent même si suspendu/graduated
                if not beneficiary:
                    beneficiary = Beneficiary.objects.filter(
                        individual=instance.individual,
                        is_deleted=False
                    ).order_by('-date_valid_from').first()
                
                if beneficiary and beneficiary.benefit_plan:
                    bp = beneficiary.benefit_plan
                    benefit_plan_data = {
                        'id': str(bp.id) if hasattr(bp, 'id') else None,
                        'code': getattr(bp, 'code', None),
                        'name': getattr(bp, 'name', None) or str(bp),
                    }
            
            # Fallback: si pas de Beneficiary trouvé, essayer via PaymentPlan (pour compatibilité)
            if not benefit_plan_data:
                if payroll_bc and payroll_bc.payroll and payroll_bc.payroll.payment_plan:
                    payment_plan = payroll_bc.payroll.payment_plan
                    if payment_plan and payment_plan.benefit_plan:
                        bp = payment_plan.benefit_plan
                        benefit_plan_data = {
                            'id': str(bp.id) if hasattr(bp, 'id') else None,
                            'code': getattr(bp, 'code', None),
                            'name': getattr(bp, 'name', None) or str(bp),
                        }
            
            data['benefit_plan'] = benefit_plan_data
            
            # Déterminer si c'est un transfert monétaire (type = 'monetary' ou amount > 0)
            data['is_monetary_transfer'] = (
                instance.type and 'monetary' in str(instance.type).lower()
            ) or (instance.amount and float(instance.amount) > 0)
            
            # Récupérer les infos du cycle de paiement
            if payroll_bc and payroll_bc.payroll and payroll_bc.payroll.payment_cycle:
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
    class PayrollBenefitConsumptionDocument(BaseSyncDocument):
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
                
                # Récupérer benefit_plan depuis les données RÉELLES : Individual -> Beneficiary -> BenefitPlan
                beneficiary = Beneficiary.objects.filter(
                    individual=instance.benefit.individual,
                    status=BeneficiaryStatus.ACTIVE,
                    is_deleted=False
                ).order_by('-date_valid_from').first()
                
                if not beneficiary:
                    beneficiary = Beneficiary.objects.filter(
                        individual=instance.benefit.individual,
                        is_deleted=False
                    ).order_by('-date_valid_from').first()
                
                if beneficiary and beneficiary.benefit_plan:
                    bp = beneficiary.benefit_plan
                    data['benefit']['benefit_plan'] = {
                        'id': str(bp.id) if hasattr(bp, 'id') else None,
                        'code': getattr(bp, 'code', None),
                        'name': getattr(bp, 'name', None) or str(bp),
                    }
                # Fallback via PaymentPlan si pas de Beneficiary
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
                return PayrollBenefitConsumption.objects.filter(payroll=related_instance)
            elif isinstance(related_instance, BenefitConsumption):
                return PayrollBenefitConsumption.objects.filter(benefit=related_instance)


    @registry.register_document
    class BenefitAttachmentDocument(BaseSyncDocument):
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
                return BenefitAttachment.objects.filter(
                    benefit__payrollbenefitconsumption__payroll=related_instance
                )
            elif isinstance(related_instance, Bill):
                return BenefitAttachment.objects.filter(bill=related_instance)
            elif isinstance(related_instance, BenefitConsumption):
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

                beneficiary = Beneficiary.objects.filter(
                    individual=instance.benefit.individual,
                    status=BeneficiaryStatus.ACTIVE,
                    is_deleted=False
                ).order_by('-date_valid_from').first()

                if not beneficiary:
                    beneficiary = Beneficiary.objects.filter(
                        individual=instance.benefit.individual,
                        is_deleted=False
                    ).order_by('-date_valid_from').first()

                if beneficiary and beneficiary.benefit_plan:
                    bp = beneficiary.benefit_plan
                    data['benefit']['benefit_plan'] = {
                        'id': str(bp.id) if hasattr(bp, 'id') else None,
                        'code': getattr(bp, 'code', None),
                        'name': getattr(bp, 'name', None) or str(bp),
                    }
                else:
                    pbc = PayrollBenefitConsumption.objects.filter(
                        benefit=instance.benefit
                    ).select_related('payroll__payment_plan__benefit_plan', 'payroll__payment_cycle').first()
                    if pbc and pbc.payroll and pbc.payroll.payment_plan and pbc.payroll.payment_plan.benefit_plan:
                        bp = pbc.payroll.payment_plan.benefit_plan
                        data['benefit']['benefit_plan'] = {
                            'id': str(bp.id) if hasattr(bp, 'id') else None,
                            'code': getattr(bp, 'code', None),
                            'name': getattr(bp, 'name', None) or str(bp),
                        }
                    if pbc and pbc.payroll and pbc.payroll.payment_cycle:
                        data['benefit']['payment_cycle_code'] = getattr(pbc.payroll.payment_cycle, 'code', None)

            return data

        def prepare_location(self, instance):
            individual = getattr(instance.benefit, 'individual', None)
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

        def prepare_payment_plan_codes(self, instance):
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
            payrolls = PayrollBenefitConsumption.objects.filter(
                benefit=instance.benefit
            ).select_related("payroll")

            names = []
            for item in payrolls:
                payroll = getattr(item, "payroll", None)
                if payroll and payroll.name:
                    names.append(payroll.name)
            return names
