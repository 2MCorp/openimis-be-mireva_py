from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mireva', '0002_mireva_evaluation'),
    ]

    operations = [
        migrations.AlterField(
            model_name='mirevaevaluation',
            name='action',
            field=models.CharField(choices=[('APPROVED', 'Claim approved'), ('REVIEW_SELECTED', 'Claim selected for review'), ('REVIEW_DOWNGRADED', 'Model rejection sent to review'), ('REJECTED', 'Claim rejected'), ('SHADOW', 'Shadow mode: claim left untouched')], max_length=20),
        ),
    ]
