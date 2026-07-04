"""Regression for the latent ``Category.Meta`` bug.

In legacy-catalog a SECOND ``class Meta`` shadowed the first, so the
``revision`` index (and any other first-Meta option) was silently dropped.
The port merges both into one Meta; these tests pin that the merge holds —
both the ``verbose_name_plural`` AND the revision index survive.
"""
import pytest
from django.db import connection

from stapel_categories.models import Category, Feature


def _revision_index_names(model):
    return {
        idx.name for idx in model._meta.indexes if list(idx.fields) == ["revision"]
    }


def test_category_meta_keeps_both_options():
    # The shadowing bug dropped whichever Meta came first. Both must be present.
    assert Category._meta.verbose_name_plural == "categories"
    assert _revision_index_names(Category) == {"cat_category_revision_idx"}


def test_feature_revision_index_present():
    assert _revision_index_names(Feature) == {"cat_feature_revision_idx"}


@pytest.mark.django_db
def test_revision_indexes_exist_in_db():
    # The index really lands in the schema (migrations applied), not just on
    # the model — the exact failure mode the shadowing bug caused.
    with connection.cursor() as cursor:
        indexes = connection.introspection.get_constraints(cursor, Category._meta.db_table)
    revision_indexed = any(
        info["index"] and info["columns"] == ["revision"] for info in indexes.values()
    )
    assert revision_indexed
