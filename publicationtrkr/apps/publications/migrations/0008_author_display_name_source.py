"""
Record why each Author.display_name holds what it does (#73).

Every existing row becomes `byline`, and that is accurate rather than a placeholder:
measured against production on 2026-09-21, no row had ever had its display_name edited
away from author_name. So this is additive, rewrites no data and changes no displayed
name. Credited rows start following their account name only when
sync_author_display_names is applied, after its preview has been reviewed.

Postgres fills the column from the default while adding it, then Django drops the
column default, as for any AddField. Reverse drops the column.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('publications', '0007_author_uuid_unique')]

    operations = [
        migrations.AddField(
            model_name='author',
            name='display_name_source',
            field=models.CharField(
                choices=[('byline', 'Byline'), ('account', 'FABRIC account name'), ('custom', 'Custom')],
                default='byline', max_length=16,
            ),
        ),
    ]
