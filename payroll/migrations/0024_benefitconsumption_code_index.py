from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payroll", "0023_paymentreport_historicalpaymentreport"),
    ]

    operations = [
        migrations.AddIndex(
            model_name="benefitconsumption",
            index=models.Index(fields=["code"], name="payroll_benefit_code_idx"),
        ),
    ]
