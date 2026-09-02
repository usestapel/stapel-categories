"""``catalog_health`` — no active dead ends.

An ACTIVE, non-deleted LEAF category with ZERO features (own or inherited) is
a dead end: a seller can pick it, and it types nothing — no form, no facets,
no validation, a listing that search cannot filter. A live classified stand
imported a catalogue whose untyped scraps landed exactly like that, and
nothing said so until sellers did.

The check reuses the library's real feature resolution
(``Category.get_all_features`` — own + inherited, override-aware), so it
cannot disagree with what the product will actually render. The command is a
GATE: it exits non-zero when any dead end exists, deliberately with no
``--allow`` escape — an allowed dead end is still a dead end, and the fix
(attach a feature, deactivate, or merge) is always available.

``load_catalog`` surfaces the same count post-apply, so the import that
CREATES dead ends says so at import time, not at the next audit.
"""
import tempfile
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from stapel_categories import catalog_fixtures as cf
from stapel_categories import catalog_load as cl
from stapel_categories.models import Category, CategoryFeature, Feature

from .test_catalog_load import _CatalogTestCase, _export, _read_json, _write_json


def _health(**kwargs):
    stdout = StringIO()
    call_command("catalog_health", stdout=stdout, **kwargs)
    return stdout.getvalue()


class CatalogHealthCommandTests(TestCase):
    def test_active_featureless_leaf_fails_the_gate_by_name(self):
        Category.objects.create(name="Scraps", slug="scraps")
        with self.assertRaises(CommandError) as ctx:
            _health()
        self.assertIn("scraps", str(ctx.exception))

    def test_leaf_with_an_own_feature_is_healthy(self):
        cat = Category.objects.create(name="Phones", slug="phones")
        feat = Feature.objects.create(
            name="Color", slug="color", config={"type": "string"}
        )
        CategoryFeature.objects.create(category=cat, feature=feat, order=0)
        out = _health()
        self.assertIn("0", out)

    def test_leaf_with_only_an_inherited_feature_is_healthy(self):
        parent = Category.objects.create(name="Electronics", slug="electronics")
        feat = Feature.objects.create(
            name="Color", slug="color", config={"type": "string"}
        )
        CategoryFeature.objects.create(category=parent, feature=feat, order=0)
        # copy_parent_features materializes the link on the child.
        Category.objects.create(name="Phones", slug="phones", tn_parent=parent)
        _health()  # no CommandError

    def test_featureless_parent_is_not_a_leaf_and_not_flagged(self):
        parent = Category.objects.create(name="Electronics", slug="electronics")
        child = Category.objects.create(
            name="Phones", slug="phones", tn_parent=parent
        )
        # Give ONLY the child a feature: the parent is featureless but has an
        # active child — a navigation node, not a dead end. The tree is clean.
        feat = Feature.objects.create(
            name="Color", slug="color", config={"type": "string"}
        )
        CategoryFeature.objects.create(category=child, feature=feat, order=0)
        _health()  # no CommandError

    def test_a_parent_whose_children_are_all_inactive_acts_as_a_leaf(self):
        parent = Category.objects.create(name="Electronics", slug="electronics")
        Category.objects.create(
            name="Phones", slug="phones", tn_parent=parent, active=False
        )
        with self.assertRaises(CommandError) as ctx:
            _health()
        self.assertIn("electronics", str(ctx.exception))

    def test_inactive_deleted_and_test_rows_are_not_dead_ends(self):
        Category.objects.create(name="Retired", slug="retired", active=False)
        Category.objects.create(name="Gone", slug="gone", deleted=True)
        Category.objects.create(name="Scratch", slug="scratch", is_test=True)
        _health()  # no CommandError


class LoadReportsDeadEndsTests(_CatalogTestCase):
    def test_an_import_that_creates_dead_ends_says_so(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            cats.append({
                "slug": "scraps", "parent_slug": "apparel", "name": "Scraps",
                "features": [],
            })
            _write_json(out, cf.CATEGORIES_FILE, cats)

            report = cl.load_catalog(out)
            self.assertFalse(report.failed)  # a gate for catalog_health, not for the load
            self.assertIn("scraps", report.dead_end_leaves)

    def test_command_summary_carries_the_count(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            cats.append({
                "slug": "scraps", "parent_slug": "apparel", "name": "Scraps",
                "features": [],
            })
            _write_json(out, cf.CATEGORIES_FILE, cats)

            stdout = StringIO()
            call_command("load_catalog", dir=out, stdout=stdout)
            text = stdout.getvalue()
            self.assertIn("dead end", text)
            self.assertIn("scraps", text)
