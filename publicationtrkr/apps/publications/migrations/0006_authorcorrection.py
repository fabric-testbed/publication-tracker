import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('publications', '0005_author_order')]

    operations = [
        migrations.CreateModel(
            name='AuthorCorrection',
            fields=[
                ('uuid', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True)),
                ('actor_uuid', models.CharField(max_length=255)),
                ('author_uuid', models.CharField(db_index=True, max_length=255)),
                ('publication_uuid', models.CharField(max_length=255)),
                ('reason', models.TextField()),
                ('before', models.JSONField()),
                ('after', models.JSONField(null=True)),
            ],
            options={'ordering': ('created', 'uuid')},
        ),
    ]
