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


class ActiveUnderInactiveParentTests(TestCase):
    """The second half of the gate: a live branch hanging off a retired one.

    ``active`` is stand-owned curation (0.15.0), and the loader's create-only
    guard is what keeps a re-import from undoing a deactivation. A guard is
    not a gate, though: it protects the one path it sits on, and a resurrection
    that arrives another way — a queryset ``.update(active=True)``, a fixture
    loaded by an older stapel-categories, a hand edit in the admin — leaves no
    trace the guard can catch.

    What such a resurrection cannot hide is the SHAPE it produces. An operator
    retires a subtree from the top; a partial resurrection re-activates rows
    underneath one that is still off, and the result is a category a seller
    can reach by search or by a saved link while the path to it is closed. So
    the gate asserts the invariant instead of the event: no active category
    under an inactive parent.
    """

    def _tree(self):
        feature = Feature.objects.create(name="Color", slug="color",
                                         config={"type": "string"})
        parent = Category.objects.create(name="Parent", slug="parent")
        child = Category.objects.create(name="Child", slug="child", tn_parent=parent)
        CategoryFeature.objects.create(category=child, feature=feature, order=0)
        return parent, child

    def test_active_child_under_inactive_parent_fails_the_gate_by_name(self):
        parent, _child = self._tree()
        parent.active = False
        parent.save()

        self.assertEqual(cl.active_under_inactive_parent(), ["child"])
        with self.assertRaises(CommandError) as ctx:
            _health()
        self.assertIn("child", str(ctx.exception))
        self.assertIn("parent", str(ctx.exception))

    def test_a_fully_retired_subtree_is_healthy(self):
        """Deactivating from the top down is the correct operation, not a
        finding — the whole point is that the gate names the INCONSISTENT
        half, so an operator who did it right sees nothing."""
        parent, child = self._tree()
        for cat in (parent, child):
            cat.active = False
            cat.save()
        self.assertEqual(cl.active_under_inactive_parent(), [])
        self.assertIn("0 dead ends", _health())

    def test_an_active_subtree_is_healthy(self):
        self._tree()
        self.assertEqual(cl.active_under_inactive_parent(), [])

    def test_deleted_and_test_rows_are_outside_the_check(self):
        """Same boundary the dead-end finder draws: canon is what is live and
        not scratch, on both sides of the relationship."""
        parent, child = self._tree()
        parent.active = False
        parent.save()

        child.is_test = True
        child.save()
        self.assertEqual(cl.active_under_inactive_parent(), [])

        child.is_test = False
        child.deleted = True
        child.save()
        self.assertEqual(cl.active_under_inactive_parent(), [])

    def test_a_resurrection_that_bypasses_the_loader_guard_is_caught(self):
        """The scenario in one test: an operator retires a subtree, something
        re-activates the leaf past the model layer, and the gate says so."""
        parent, child = self._tree()
        for cat in (parent, child):
            cat.active = False
            cat.save()
        self.assertEqual(cl.active_under_inactive_parent(), [])

        # Not through save(): the queryset write no guard can see.
        Category.objects.filter(slug="child").update(active=True)

        self.assertEqual(cl.active_under_inactive_parent(), ["child"])
        with self.assertRaises(CommandError):
            _health()
