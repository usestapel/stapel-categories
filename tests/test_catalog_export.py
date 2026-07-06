"""Tests for ``export_catalog`` / catalog fixture serialization (CAT-1).

Covers the invariants from docs/catalog-fixtures-sync.md §6:
- is_test exclusion is transitive (category, feature, and CategoryFeature link)
- export is byte-stable (double run yields byte-identical files)
- natural keys (slug / parent_slug; reference vs inline override)
- tree with inheritance + override features
- empty catalog
"""
import json
import os
import tempfile

from django.core.management import call_command
from django.test import TestCase

from stapel_categories import catalog_fixtures as cf
from stapel_categories.models import Category, CategoryFeature, Feature


def _export(out_dir, **kwargs):
    call_command("export_catalog", out=out_dir, **kwargs)


def _read(out_dir, name):
    with open(os.path.join(out_dir, name), encoding="utf-8") as fh:
        return fh.read()


def _load(out_dir, name):
    return json.loads(_read(out_dir, name))


class EmptyCatalogTests(TestCase):
    def test_empty_catalog_exports_empty_lists(self):
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            self.assertEqual(_load(out, cf.FEATURES_FILE), [])
            self.assertEqual(_load(out, cf.CATEGORIES_FILE), [])
            state = _load(out, cf.STATE_FILE)
            self.assertEqual(state["features"], {})
            self.assertEqual(state["categories"], {})
            self.assertEqual(state["version"], cf.STATE_VERSION)


class NaturalKeyTests(TestCase):
    def setUp(self):
        self.color = Feature.objects.create(
            name="Color", slug="color", config={"type": "select", "options": ["red", "blue"]},
            show_as_badge=True,
        )
        self.size = Feature.objects.create(
            name="Size", slug="size", config={"type": "int", "min": 0},
        )
        self.electronics = Category.objects.create(name="Electronics", slug="electronics")
        CategoryFeature.objects.create(category=self.electronics, feature=self.color, order=0)
        CategoryFeature.objects.create(category=self.electronics, feature=self.size, order=1)

    def test_features_keyed_by_slug(self):
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            features = _load(out, cf.FEATURES_FILE)
            slugs = [f["slug"] for f in features]
            self.assertEqual(slugs, ["color", "size"])  # sorted by slug
            color = next(f for f in features if f["slug"] == "color")
            self.assertEqual(color["name"], "Color")
            self.assertEqual(color["config"], {"type": "select", "options": ["red", "blue"]})
            self.assertTrue(color["show_as_badge"])
            self.assertNotIn("is_test", color)  # default export never writes is_test

    def test_category_has_parent_slug_and_feature_refs(self):
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _load(out, cf.CATEGORIES_FILE)
            self.assertEqual(len(cats), 1)
            electronics = cats[0]
            self.assertEqual(electronics["slug"], "electronics")
            self.assertIsNone(electronics["parent_slug"])
            # Shared root features are exported as bare {"slug"} references,
            # ordered by the materialized CategoryFeature order.
            self.assertEqual(
                electronics["features"],
                [{"slug": "color"}, {"slug": "size"}],
            )

    def test_child_category_addresses_parent_by_slug(self):
        phones = Category.objects.create(name="Phones", slug="phones", tn_parent=self.electronics)
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _load(out, cf.CATEGORIES_FILE)
            by_slug = {c["slug"]: c for c in cats}
            self.assertEqual(by_slug["phones"]["parent_slug"], "electronics")
            # parent sorts before child (depth ordering)
            self.assertLess(
                [c["slug"] for c in cats].index("electronics"),
                [c["slug"] for c in cats].index("phones"),
            )
            self.assertEqual(phones.slug, "phones")  # sanity


class OverrideInheritanceTests(TestCase):
    def setUp(self):
        self.color = Feature.objects.create(
            name="Color", slug="color", config={"type": "select", "options": ["red"]},
        )
        self.parent = Category.objects.create(name="Parent", slug="parent")
        CategoryFeature.objects.create(category=self.parent, feature=self.color, order=0)
        # Child copies the parent link (copy_parent_features).
        self.child = Category.objects.create(name="Child", slug="child", tn_parent=self.parent)
        # Build an override row (tn_parent set, same slug, different config) —
        # what the feature editor's `inherit` action produces — and repoint the
        # child's link at it.
        self.override = Feature.objects.create(
            tn_parent=self.color, name="Color", slug="color",
            config={"type": "select", "options": ["red", "green", "blue"]},
            mandatory=True,
        )
        link = self.child.category_features.get(feature=self.color)
        link.feature = self.override
        link.save()

    def test_parent_uses_reference_child_inlines_override(self):
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = {c["slug"]: c for c in _load(out, cf.CATEGORIES_FILE)}
            # Parent: shared root -> bare reference.
            self.assertEqual(cats["parent"]["features"], [{"slug": "color"}])
            # Child: override row -> inline config with display flags.
            child_feats = cats["child"]["features"]
            self.assertEqual(len(child_feats), 1)
            entry = child_feats[0]
            self.assertEqual(entry["slug"], "color")
            self.assertEqual(entry["config"]["options"], ["red", "green", "blue"])
            self.assertTrue(entry["mandatory"])

    def test_override_not_listed_in_features_json(self):
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            features = _load(out, cf.FEATURES_FILE)
            # Only the root canonical "color" is a features.json entry (the
            # override is a child tree node, inlined per-category instead).
            self.assertEqual([f["slug"] for f in features], ["color"])
            self.assertEqual(features[0]["config"]["options"], ["red"])


class IsTestExclusionTests(TestCase):
    def test_is_test_category_excluded(self):
        Category.objects.create(name="Real", slug="real")
        Category.objects.create(name="Scratch", slug="scratch", is_test=True)
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            slugs = [c["slug"] for c in _load(out, cf.CATEGORIES_FILE)]
            self.assertEqual(slugs, ["real"])

    def test_is_test_feature_excluded_from_features_json(self):
        Feature.objects.create(name="Real", slug="real_feat", config={"type": "string"})
        Feature.objects.create(name="Test", slug="test_feat", config={"type": "string"}, is_test=True)
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            slugs = [f["slug"] for f in _load(out, cf.FEATURES_FILE)]
            self.assertEqual(slugs, ["real_feat"])

    def test_is_test_feature_excluded_transitively_from_category_link(self):
        """A test feature linked to a real category is dropped from its list."""
        real_feat = Feature.objects.create(name="Real", slug="real_feat", config={"type": "string"})
        test_feat = Feature.objects.create(
            name="Test", slug="test_feat", config={"type": "string"}, is_test=True,
        )
        cat = Category.objects.create(name="Cat", slug="cat")
        CategoryFeature.objects.create(category=cat, feature=real_feat, order=0)
        CategoryFeature.objects.create(category=cat, feature=test_feat, order=1)
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _load(out, cf.CATEGORIES_FILE)
            self.assertEqual(cats[0]["features"], [{"slug": "real_feat"}])

    def test_include_test_flag_includes_everything(self):
        Feature.objects.create(name="Test", slug="test_feat", config={"type": "string"}, is_test=True)
        Category.objects.create(name="Scratch", slug="scratch", is_test=True)
        with tempfile.TemporaryDirectory() as out:
            _export(out, include_test=True)
            fslugs = [f["slug"] for f in _load(out, cf.FEATURES_FILE)]
            cslugs = [c["slug"] for c in _load(out, cf.CATEGORIES_FILE)]
            self.assertIn("test_feat", fslugs)
            self.assertIn("scratch", cslugs)
            # is_test round-trips into the debug dump.
            tf = next(f for f in _load(out, cf.FEATURES_FILE) if f["slug"] == "test_feat")
            self.assertTrue(tf["is_test"])


class ByteStabilityTests(TestCase):
    def setUp(self):
        color = Feature.objects.create(name="Color", slug="color", config={"type": "select", "options": ["b", "a"]})
        Feature.objects.create(name="Size", slug="size", config={"type": "int"})
        electronics = Category.objects.create(name="Electronics", slug="electronics")
        CategoryFeature.objects.create(category=electronics, feature=color, order=0)
        Category.objects.create(name="Phones", slug="phones", tn_parent=electronics)
        Category.objects.create(name="Apparel", slug="apparel")

    def test_double_export_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            _export(a)
            _export(b)
            for name in (cf.FEATURES_FILE, cf.CATEGORIES_FILE, cf.STATE_FILE):
                self.assertEqual(_read(a, name), _read(b, name), f"{name} not byte-stable")

    def test_files_end_with_trailing_newline(self):
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            for name in (cf.FEATURES_FILE, cf.CATEGORIES_FILE):
                self.assertTrue(_read(out, name).endswith("\n"))


class SidecarAndPrefilterTests(TestCase):
    def test_sidecar_has_content_hash_per_natural_key(self):
        Feature.objects.create(name="Color", slug="color", config={"type": "string"})
        Category.objects.create(name="Electronics", slug="electronics")
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            state = _load(out, cf.STATE_FILE)
            self.assertIn("color", state["features"])
            self.assertTrue(state["features"]["color"].startswith("sha256:"))
            self.assertIn("electronics", state["categories"])
            self.assertGreaterEqual(state["max_revision"], 1)

    def test_prefilter_skips_when_revision_unchanged(self):
        Category.objects.create(name="Electronics", slug="electronics")
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            first = _read(out, cf.CATEGORIES_FILE)
            # Second run with no DB change: pre-filter reports nothing to write,
            # existing files stay identical.
            _export(out)
            self.assertEqual(_read(out, cf.CATEGORIES_FILE), first)


class FilteredParentResolutionTests(TestCase):
    """A ``parent_slug`` must always reference a record in the same file.

    The physical ``tn_parent`` chain can pass through rows the export filters
    out (soft-deleted / is_test categories with live children — a state
    ``load_catalog --deletions soft`` itself produces). Naively exporting
    ``tn_parent.slug`` wrote a dangling reference, so the default export was
    not loadable into a fresh DB (fable review, H).
    """

    def test_soft_deleted_parent_child_reparents_to_nearest_live_ancestor(self):
        grandparent = Category.objects.create(name="GP", slug="gp")
        parent = Category.objects.create(name="P", slug="p", tn_parent=grandparent)
        Category.objects.create(name="C", slug="c", tn_parent=parent)
        parent.soft_delete()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = {c["slug"]: c for c in _load(out, cf.CATEGORIES_FILE)}
            self.assertNotIn("p", cats)  # soft-deleted rows are absent
            self.assertEqual(cats["c"]["parent_slug"], "gp")

    def test_soft_deleted_root_parent_child_becomes_root(self):
        parent = Category.objects.create(name="P", slug="p")
        Category.objects.create(name="C", slug="c", tn_parent=parent)
        parent.soft_delete()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = {c["slug"]: c for c in _load(out, cf.CATEGORIES_FILE)}
            self.assertIsNone(cats["c"]["parent_slug"])

    def test_is_test_parent_child_reparents_past_it(self):
        root = Category.objects.create(name="Live root", slug="live-root")
        qa = Category.objects.create(name="QA", slug="qa", tn_parent=root, is_test=True)
        Category.objects.create(name="Real", slug="real-child", tn_parent=qa)
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = {c["slug"]: c for c in _load(out, cf.CATEGORIES_FILE)}
            self.assertNotIn("qa", cats)
            self.assertEqual(cats["real-child"]["parent_slug"], "live-root")

    def test_include_test_keeps_the_test_parent_edge(self):
        qa = Category.objects.create(name="QA", slug="qa", is_test=True)
        Category.objects.create(name="Real", slug="real-child", tn_parent=qa)
        with tempfile.TemporaryDirectory() as out:
            _export(out, include_test=True)
            cats = {c["slug"]: c for c in _load(out, cf.CATEGORIES_FILE)}
            self.assertEqual(cats["real-child"]["parent_slug"], "qa")


class IncludeTestGuardTests(TestCase):
    def test_include_test_without_out_refuses_to_clobber_canon(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("export_catalog", include_test=True)

    def test_include_test_with_explicit_out_is_allowed(self):
        Category.objects.create(name="QA", slug="qa", is_test=True)
        with tempfile.TemporaryDirectory() as out:
            _export(out, include_test=True)
            cats = {c["slug"]: c for c in _load(out, cf.CATEGORIES_FILE)}
            self.assertIn("qa", cats)
