"""Catalog fixtures carry rules, form metadata and ``external_id`` (spec §5).

The fixture pair is the import format: the catalogue importer writes it and
``load_catalog`` applies it, so a field the export writes but the loader does
not read (or vice versa) is a field that survives review and then vanishes on
the stand. The three properties that keep the pair honest — byte-stability,
an idempotent second load, and a sidecar hash that actually covers the record
— are asserted here against a catalog whose features use every new field, on
both a root feature and an inline (per-category) override.
"""
import json
import os
import tempfile

from django.test import TestCase

from stapel_categories import catalog_fixtures as cf
from stapel_categories.catalog_load import SKIPPED, load_catalog
from stapel_categories.models import Category, CategoryFeature, Feature

RULES = [
    {
        "effect": "require",
        "when": {"all": [{"feature": "condition", "op": "in", "values": ["used"]}]},
    }
]
OVERRIDE_RULES = [
    {
        "effect": "hide",
        "when": {"any": [{"feature": "condition", "op": "empty"}]},
    }
]
HINTS = [{"title": "hint.title", "content": "hint.content"}]


def _export(out_dir):
    features, categories, state = cf.build_catalog()
    for name, payload in (
        (cf.FEATURES_FILE, features),
        (cf.CATEGORIES_FILE, categories),
        (cf.STATE_FILE, state),
    ):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
            fh.write(cf.canonical_json(payload))
    return features, categories, state


def _read(out_dir, name):
    with open(os.path.join(out_dir, name), encoding="utf-8") as fh:
        return fh.read()


class RulesAndMetadataRoundTripTests(TestCase):
    """A root feature and a per-category override, both fully populated."""

    def setUp(self):
        self.screen = Feature.objects.create(
            name="Screen condition",
            slug="screen_condition",
            config={"type": "string", "maxLength": 100},
            rules=RULES,
            description="feature.screen_condition.help",
            example="No scratches",
            default=["intact"],
            hints=HINTS,
            group="About the condition",
        )
        self.phones = Category.objects.create(
            name="Phones", slug="phones", external_id="129639"
        )
        CategoryFeature.objects.create(category=self.phones, feature=self.screen, order=0)

        # A per-category variation of the same slug: the override row is a
        # child of the root, so export inlines it into the category record.
        self.override = Feature.objects.create(
            tn_parent=self.screen,
            name="Screen condition",
            slug="screen_condition",
            config={"type": "string", "maxLength": 40},
            rules=OVERRIDE_RULES,
            description="feature.screen_condition.used_help",
            example="Hairline crack, top left",
            default=None,
            hints=[{"title": "hint.used.title", "content": "hint.used.content"}],
            group="About this phone",
        )
        self.used_phones = Category.objects.create(
            name="Used phones", slug="used-phones", tn_parent=self.phones, external_id="129640"
        )
        self.used_phones.category_features.all().delete()  # drop the copied parent link
        CategoryFeature.objects.create(
            category=self.used_phones, feature=self.override, order=0
        )

    # ── shape ────────────────────────────────────────────────────────

    def test_the_root_feature_record_carries_every_new_field(self):
        features, _, _ = cf.build_catalog()
        (record,) = features
        self.assertEqual(record["rules"], RULES)
        self.assertEqual(record["description"], "feature.screen_condition.help")
        self.assertEqual(record["example"], "No scratches")
        self.assertEqual(record["default"], ["intact"])
        self.assertEqual(record["hints"], HINTS)
        self.assertEqual(record["group"], "About the condition")

    def test_the_inline_override_carries_every_new_field(self):
        _, categories, _ = cf.build_catalog()
        used = next(c for c in categories if c["slug"] == "used-phones")
        (entry,) = used["features"]
        self.assertEqual(entry["slug"], "screen_condition")
        self.assertEqual(entry["rules"], OVERRIDE_RULES)
        self.assertEqual(entry["description"], "feature.screen_condition.used_help")
        self.assertEqual(entry["example"], "Hairline crack, top left")
        self.assertIsNone(entry["default"])
        self.assertEqual(entry["hints"], [{"title": "hint.used.title", "content": "hint.used.content"}])
        self.assertEqual(entry["group"], "About this phone")

    def test_the_category_record_carries_external_id(self):
        _, categories, _ = cf.build_catalog()
        by_slug = {c["slug"]: c for c in categories}
        self.assertEqual(by_slug["phones"]["external_id"], "129639")
        self.assertEqual(by_slug["used-phones"]["external_id"], "129640")

    # ── the three fixture invariants ─────────────────────────────────

    def test_export_is_byte_stable(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            _export(first)
            _export(second)
            for name in (cf.FEATURES_FILE, cf.CATEGORIES_FILE, cf.STATE_FILE):
                self.assertEqual(_read(first, name), _read(second, name))

    def test_the_sidecar_hash_covers_the_new_fields(self):
        """A hash blind to a field lets that field drift without a conflict.

        The 3-way diff is hash-only, so if ``rules`` were outside the hashed
        record a fixture could change a rule set and the loader would classify
        the record as unchanged and skip it forever.
        """
        _, _, before = cf.build_catalog()
        self.screen.rules = []
        self.screen.save()
        _, _, after = cf.build_catalog()
        self.assertNotEqual(before["features"]["screen_condition"], after["features"]["screen_condition"])

        self.override.group = "Something else"
        self.override.save()
        _, _, later = cf.build_catalog()
        self.assertNotEqual(after["categories"]["used-phones"], later["categories"]["used-phones"])

    def test_the_sidecar_hash_covers_external_id(self):
        _, _, before = cf.build_catalog()
        self.phones.external_id = "999"
        self.phones.save()
        _, _, after = cf.build_catalog()
        self.assertNotEqual(before["categories"]["phones"], after["categories"]["phones"])

    def test_export_load_round_trip_restores_everything(self):
        with tempfile.TemporaryDirectory() as out:
            _export(out)

            Category.objects.all().delete()
            Feature.objects.all().delete()

            # seed_if_empty: with the sidecar in hand an empty DB otherwise
            # reads as a deliberate local deletion, not as a fresh stand.
            report = load_catalog(out, seed_if_empty=True)
            self.assertFalse(report.failed, report.categories + report.features)

            root = Feature.objects.get(slug="screen_condition", tn_parent__isnull=True)
            self.assertEqual(root.rules, RULES)
            self.assertEqual(root.description, "feature.screen_condition.help")
            self.assertEqual(root.example, "No scratches")
            self.assertEqual(root.default, ["intact"])
            self.assertEqual(root.hints, HINTS)
            self.assertEqual(root.group, "About the condition")

            override = Feature.objects.get(slug="screen_condition", tn_parent=root)
            self.assertEqual(override.rules, OVERRIDE_RULES)
            self.assertEqual(override.config, {"type": "string", "maxLength": 40})
            self.assertIsNone(override.default)
            self.assertEqual(override.group, "About this phone")

            self.assertEqual(Category.objects.get(slug="phones").external_id, "129639")
            self.assertEqual(Category.objects.get(slug="used-phones").external_id, "129640")

    def test_a_second_load_is_all_skips(self):
        """Idempotency: nothing written, no revision bump, no event."""
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            Category.objects.all().delete()
            Feature.objects.all().delete()
            load_catalog(out, seed_if_empty=True)

            revisions = sorted(Feature.objects.values_list("slug", "revision"))
            report = load_catalog(out)

            self.assertFalse(report.failed)
            kinds = {item.kind for item in report.features + report.categories}
            self.assertEqual(kinds, {SKIPPED}, [(i.kind, i.key) for i in report.features + report.categories])
            self.assertEqual(sorted(Feature.objects.values_list("slug", "revision")), revisions)

    def test_a_changed_rule_set_is_applied_and_then_settles(self):
        """The loader must both notice the edit and stop noticing it after.

        ``default`` is the trap here: ``None`` is a real value ("start
        empty"), not "this record says nothing", so a loader treating the two
        the same would keep re-detecting the change on every run.
        """
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            path = os.path.join(out, cf.FEATURES_FILE)
            records = json.loads(_read(out, cf.FEATURES_FILE))
            records[0]["rules"] = OVERRIDE_RULES
            records[0]["default"] = None
            records[0]["group"] = "Rewritten section"
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(cf.canonical_json(records))

            report = load_catalog(out)
            self.assertFalse(report.failed, [(i.kind, i.key, i.detail) for i in report.features])

            root = Feature.objects.get(slug="screen_condition", tn_parent__isnull=True)
            self.assertEqual(root.rules, OVERRIDE_RULES)
            self.assertIsNone(root.default)
            self.assertEqual(root.group, "Rewritten section")

            again = load_catalog(out)
            self.assertEqual(
                {item.kind for item in again.features + again.categories}, {SKIPPED}
            )

    def test_a_changed_inline_override_is_applied_and_then_settles(self):
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            path = os.path.join(out, cf.CATEGORIES_FILE)
            records = json.loads(_read(out, cf.CATEGORIES_FILE))
            used = next(r for r in records if r["slug"] == "used-phones")
            used["features"][0]["description"] = "feature.screen_condition.rewritten"
            used["features"][0]["hints"] = []
            used["external_id"] = "777"
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(cf.canonical_json(records))

            report = load_catalog(out)
            self.assertFalse(report.failed, [(i.kind, i.key, i.detail) for i in report.categories])

            override = Feature.objects.get(slug="screen_condition", tn_parent__isnull=False)
            self.assertEqual(override.description, "feature.screen_condition.rewritten")
            self.assertEqual(override.hints, [])
            self.assertEqual(Category.objects.get(slug="used-phones").external_id, "777")

            again = load_catalog(out)
            self.assertEqual(
                {item.kind for item in again.features + again.categories}, {SKIPPED}
            )


class SidecarVersionTests(TestCase):
    """A stale sidecar's hashes no longer describe a current record.

    v1→v2 (0.7.0): the records grew fields. v2→v3 (0.13.0): category hashes
    are now computed over the sync view (presentation keys stripped —
    ``cf.category_sync_view``), so every category hash a v2 sidecar holds
    would read as a phantom two-sided change on every key. v3 -> v4 is the
    same story one field over: ``active`` left the hashed subset too.
    """

    def test_an_older_sidecar_is_refused_loudly(self):
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            path = os.path.join(out, cf.STATE_FILE)
            state = json.loads(_read(out, cf.STATE_FILE))
            state["version"] = 1
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(cf.canonical_json(state))

            with self.assertRaises(ValueError) as ctx:
                load_catalog(out)
            self.assertIn("incompatible .sync-state.json version", str(ctx.exception))

    def test_the_current_version_is_four(self):
        """Bumped to 4 in 0.15.0: ``active`` joined the sync view's
        exclusions (``cf.CURATION_KEYS``), so a v3 hash covers a different
        subset of the record and must be refused rather than compared."""
        self.assertEqual(cf.STATE_VERSION, 4)
