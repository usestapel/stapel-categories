# Expand-only: the slug column grows, a table is added, nothing is dropped.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("categories", "0009_feature_axis_role"),
    ]

    operations = [
        migrations.AlterField(
            model_name="category",
            name="slug",
            field=models.CharField(db_index=True, max_length=255, unique=True),
        ),
        migrations.CreateModel(
            name="CategorySlugAlias",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("slug", models.CharField(max_length=255, unique=True)),
                ("retired_at", models.DateTimeField(auto_now=True)),
                ("category", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="slug_aliases", to="categories.category")),
            ],
            options={
                "verbose_name": "category slug alias",
                "verbose_name_plural": "category slug aliases",
            },
        ),
    ]
