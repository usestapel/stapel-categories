# Expand-only: two blank-able columns are added and a table is created;
# nothing is dropped and no existing row changes meaning (both defaults
# are the empty string, which is today's behaviour).

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('categories', '0010_category_slug_alias'),
    ]

    operations = [
        migrations.AddField(
            model_name='category',
            name='children_axis_tag',
            field=models.CharField(blank=True, default='', help_text="External tag of the field whose values this category's children enumerate (e.g. `operation_type`). Not a translation key — the source catalogue's own identifier, so the same tag can be recognised where it rides a leaf as an ordinary feature.", max_length=64),
        ),
        migrations.AddField(
            model_name='category',
            name='children_expand_by',
            field=models.CharField(blank=True, default='', help_text="Slug of one of this category's features whose closed value set IS this category's children. The reads answer with virtual children (a `{feature: value}` filter each), and nothing is written to the table. A category with real children cannot carry this.", max_length=100),
        ),
        migrations.CreateModel(
            name='CategoryLink',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('order', models.PositiveIntegerField(default=0, help_text="Position among the source's children — the pointer is inserted at this index into the child list, not appended after it.")),
                ('label', models.CharField(blank=True, default='', help_text="Text drawn on the pointer — a translation key, like `name`. Empty means the target's own name.", max_length=200)),
                ('external_source', models.CharField(blank=True, default='', help_text="Who created this link: an importer's name, or `storefront` for an operator's own. A catalogue reload rewrites only the links carrying the source it loads.", max_length=32)),
                ('source', models.ForeignKey(help_text='The category whose children this pointer is drawn among.', on_delete=django.db.models.deletion.CASCADE, related_name='links', to='categories.category')),
                ('target', models.ForeignKey(help_text='The category the pointer leads to.', on_delete=django.db.models.deletion.CASCADE, related_name='linked_from', to='categories.category')),
            ],
            options={
                'ordering': ['order', 'id'],
                'unique_together': {('source', 'target')},
            },
        ),
    ]
