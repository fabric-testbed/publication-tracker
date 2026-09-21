from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('publications', '0006_authorcorrection')]

    operations = [
        migrations.AlterField(
            model_name='author', name='uuid',
            field=models.CharField(max_length=255, unique=True),
        ),
    ]
