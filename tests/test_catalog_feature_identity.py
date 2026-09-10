"""``load_catalog`` and a feature's IDENTITY — the question a field asks.

The stand, 2026-09-10. A `--dry-run --keep-slugs` plan said ``features:
updated 2`` and listed ``~ make`` and ``~ make_ref_select``. Behind those two
lines the fixture was about to point the live ``make`` feature at a different
vocabulary, flip ``mandatory`` and inject ``rules``. Every car listing's brand
answer keys into the OLD vocabulary, so the load would have emptied the brand
facet, the AI fill and the brand links — and a ``~`` line is how a description
typo and a vocabulary swap both look.

These tests hold what replaced that: a matched feature whose ``config.type`` or
``config.optionsRef`` moves is refused and named, the escape says it out loud,
and a dry-run ``~`` line names the fields it is about to write.
"""
import io
import tempfile

from django.core.management import call_command
from django.core.management.base import CommandError
from stapel_attributes.vocabularies import (
    VocabularyInfo,
    VocabularyLevel,
    register_vocabulary_resolver,
)

from stapel_categories import catalog_fixtures as cf
from stapel_categories import catalog_load as cl
from stapel_categories.models import Category, CategoryFeature, Feature

from .fake_vocabulary import VOCABULARY, FakeVocabularyResolver
from .test_catalog_load import _CatalogTestCase, _export, _read_json, _write_json

#: The second catalogue the fixture tries to move the feature onto.
OTHER = "cars"
OTHER_LEVELS = (VocabularyLevel(name="Make"),)
OTHER_TERMS = {"Make": {"alfa": "Alfa Romeo", "beta": "Beta"}}


class _TwoVocabularyResolver(FakeVocabularyResolver):
    """The fake, plus a second catalogue — both valid, which is the point.

    A swap has to be refused because it is a swap, not because the target
    happens to be unknown.
    """

    def describe(self, vocabulary):
        if vocabulary == OTHER:
            return VocabularyInfo(slug=OTHER, levels=OTHER_LEVELS)
        return super().describe(vocabulary)

    def exists(self, vocabulary, level, code):
        if vocabulary == OTHER:
            return code in OTHER_TERMS.get(level, {})
        return super().exists(vocabulary, level, code)

    def is_child(self, vocabulary, level, code, parent_level, parent_code):
        if vocabulary == OTHER:
            return False
        return super().is_child(vocabulary, level, code, parent_level, parent_code)

    def labels(self, vocabulary, level, codes):
        if vocabulary == OTHER:
            known = OTHER_TERMS.get(level, {})
            return {code: known[code] for code in codes if code in known}
        return super().labels(vocabulary, level, codes)


def _ref(vocabulary=VOCABULARY, level="Vendor") -> dict:
    return {"type": "ref_select", "optionsRef": {"vocabulary": vocabulary, "level": level}}


class _IdentityCase(_CatalogTestCase):
    """One referential feature on one category, exported."""

    def setUp(self):
        super().setUp()
        register_vocabulary_resolver(_TwoVocabularyResolver())
        self.addCleanup(register_vocabulary_resolver, None)
        self.make = Feature.objects.create(
            name="Make", slug="make", config=_ref(), mandatory=True,
        )
        self.cars = Category.objects.create(name="Cars", slug="cars")
        CategoryFeature.objects.create(category=self.cars, feature=self.make, order=0)
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.directory = self._dir.name
        _export(self.directory)

    def edit_feature(self, **changes):
        records = _read_json(self.directory, cf.FEATURES_FILE)
        for record in records:
            if record["slug"] == "make":
                record.update(changes)
        _write_json(self.directory, cf.FEATURES_FILE, records)

    def load(self, **kwargs):
        kwargs.setdefault("on_conflict", cl.ON_CONFLICT_FIXTURE)
        return cl.load_catalog(self.directory, **kwargs)

    def live(self) -> Feature:
        return Feature.objects.get(slug="make", tn_parent__isnull=True)


class RefusalTests(_IdentityCase):
    def test_a_vocabulary_swap_is_refused_and_the_row_is_untouched(self):
        self.edit_feature(config=_ref(OTHER, "Make"), mandatory=False)

        report = self.load()

        self.assertTrue(report.failed)
        self.assertEqual(len(report.feature_identity_changes), 1)
        change = report.feature_identity_changes[0]
        self.assertEqual(change.key, "make")
        self.assertFalse(change.applied)
        self.assertIn("vocabulary 'phones' → 'cars'", change.detail)
        self.assertIn("level 'Vendor' → 'Make'", change.detail)
        # Nothing was written: the answers still key into the live vocabulary.
        live = self.live()
        self.assertEqual(live.config["optionsRef"]["vocabulary"], VOCABULARY)
        self.assertTrue(live.mandatory)
        kinds = {it.key: it.kind for it in report.features}
        self.assertEqual(kinds["make"], cl.IDENTITY_BLOCKED)

    def test_a_type_change_is_refused_too(self):
        self.edit_feature(config={"type": "string", "maxLength": 64})

        report = self.load()

        self.assertTrue(report.failed)
        self.assertIn(
            "config.type 'ref_select' → 'string'",
            report.feature_identity_changes[0].detail,
        )
        self.assertEqual(self.live().config["type"], "ref_select")

    def test_the_refusal_does_not_re_plan_as_synced(self):
        """A refused record keeps its old base, so it is offered again."""
        self.edit_feature(config=_ref(OTHER, "Make"))
        self.load()

        again = self.load(dry_run=True)

        self.assertEqual(len(again.feature_identity_changes), 1)

    def test_a_description_change_on_the_same_vocabulary_is_a_plain_update(self):
        self.edit_feature(description="feature.make.help", mandatory=False)

        report = self.load()

        self.assertFalse(report.failed, [(i.kind, i.key, i.detail) for i in report.features])
        self.assertEqual(report.feature_identity_changes, [])
        live = self.live()
        self.assertEqual(live.description, "feature.make.help")
        self.assertFalse(live.mandatory)
        self.assertEqual(live.config["optionsRef"]["vocabulary"], VOCABULARY)

    def test_the_command_names_the_refusal_under_its_own_heading(self):
        self.edit_feature(config=_ref(OTHER, "Make"))

        buf = io.StringIO()
        with self.assertRaises(CommandError):
            call_command(
                "load_catalog", dir=self.directory,
                on_conflict=cl.ON_CONFLICT_FIXTURE, stdout=buf,
            )

        text = buf.getvalue()
        self.assertIn("feature identity changes REFUSED: 1", text)
        self.assertIn(
            "make: vocabulary 'phones' → 'cars', level 'Vendor' → 'Make'", text
        )
        self.assertIn("--allow-feature-identity-change", text)
        # …and the record's own line says it was not written.
        self.assertIn(
            "! make  (vocabulary 'phones' → 'cars', level 'Vendor' → 'Make'"
            " — refused, not written)",
            text,
        )


class EscapeTests(_IdentityCase):
    def test_the_escape_applies_the_swap_and_lists_it_as_applied(self):
        self.edit_feature(config=_ref(OTHER, "Make"), mandatory=False)

        report = self.load(allow_feature_identity_change=True)

        self.assertFalse(report.failed, [(i.kind, i.key, i.detail) for i in report.features])
        self.assertEqual(len(report.feature_identity_changes), 1)
        self.assertTrue(report.feature_identity_changes[0].applied)
        live = self.live()
        self.assertEqual(live.config["optionsRef"]["vocabulary"], OTHER)
        self.assertEqual(live.config["optionsRef"]["level"], "Make")
        self.assertFalse(live.mandatory)

    def test_the_command_lists_what_it_applied(self):
        self.edit_feature(config=_ref(OTHER, "Make"))

        buf = io.StringIO()
        call_command(
            "load_catalog", dir=self.directory, allow_feature_identity_change=True,
            on_conflict=cl.ON_CONFLICT_FIXTURE, stdout=buf,
        )

        text = buf.getvalue()
        self.assertIn(
            "feature identity changes APPLIED "
            "(--allow-feature-identity-change): 1",
            text,
        )
        self.assertIn("make: vocabulary 'phones' → 'cars'", text)


class DryRunFieldTests(_IdentityCase):
    def test_the_dry_run_line_names_the_changed_fields(self):
        self.edit_feature(
            config=_ref(OTHER, "Make"), mandatory=False, name="Brand",
        )

        report = self.load(dry_run=True, allow_feature_identity_change=True)

        item = next(it for it in report.features if it.key == "make")
        self.assertEqual(item.kind, cl.UPDATED)
        self.assertEqual(item.detail, "config.optionsRef, mandatory, name")

    def test_the_command_prints_the_field_set(self):
        self.edit_feature(description="feature.make.help")

        buf = io.StringIO()
        call_command(
            "load_catalog", dir=self.directory, dry_run=True,
            on_conflict=cl.ON_CONFLICT_FIXTURE, stdout=buf,
        )

        self.assertIn("~ make  (description)", buf.getvalue())


class _OverrideCase(_CatalogTestCase):
    """The client's shape: the question lives on the per-category OVERRIDE.

    The root sits on one vocabulary level and the row the leaf actually uses
    is an override two levels down on another — so a swap that never touches
    ``features.json`` still moves what every listing in that leaf answered.
    """

    def setUp(self):
        super().setUp()
        register_vocabulary_resolver(_TwoVocabularyResolver())
        self.addCleanup(register_vocabulary_resolver, None)
        self.root = Feature.objects.create(
            name="Make", slug="make", config=_ref(VOCABULARY, "Vendor"),
        )
        self.cars = Category.objects.create(name="Cars", slug="cars")
        CategoryFeature.objects.create(category=self.cars, feature=self.root, order=0)
        self.used = Category.objects.create(name="Used", slug="used", tn_parent=self.cars)
        self.override = Feature.objects.create(
            tn_parent=self.root, name="Make", slug="make",
            config=_ref(VOCABULARY, "Model"),
        )
        link = self.used.category_features.get(feature=self.root)
        link.feature = self.override
        link.save()
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.directory = self._dir.name
        _export(self.directory)

    def edit_entry(self, category="used", slug="make", **changes):
        records = _read_json(self.directory, cf.CATEGORIES_FILE)
        for record in records:
            if record["slug"] != category:
                continue
            for entry in record.get("features") or ():
                if entry.get("slug") == slug:
                    entry.update(changes)
        _write_json(self.directory, cf.CATEGORIES_FILE, records)

    def load(self, **kwargs):
        kwargs.setdefault("on_conflict", cl.ON_CONFLICT_FIXTURE)
        return cl.load_catalog(self.directory, **kwargs)

    def live_override(self) -> Feature:
        return Feature.objects.get(pk=self.override.pk)


class OverrideRefusalTests(_OverrideCase):
    def test_an_override_swap_is_refused_and_the_live_row_is_untouched(self):
        self.edit_entry(config=_ref(OTHER, "Make"))

        report = self.load()

        self.assertTrue(report.failed)
        self.assertEqual(len(report.feature_identity_changes), 1)
        change = report.feature_identity_changes[0]
        self.assertEqual(change.key, "used/make")
        self.assertFalse(change.applied)
        self.assertEqual(
            change.detail, "vocabulary 'phones' → 'cars', level 'Model' → 'Make'"
        )
        live = self.live_override()
        self.assertEqual(live.config["optionsRef"]["vocabulary"], VOCABULARY)
        self.assertEqual(live.config["optionsRef"]["level"], "Model")

    def test_the_rest_of_the_record_still_applies(self):
        """Only the entry is reverted — the category takes everything else."""
        self.edit_entry(config=_ref(OTHER, "Make"))
        records = _read_json(self.directory, cf.CATEGORIES_FILE)
        for record in records:
            if record["slug"] == "used":
                record["name"] = "Used cars"
        _write_json(self.directory, cf.CATEGORIES_FILE, records)

        self.load()

        self.assertEqual(Category.objects.get(pk=self.used.pk).name, "Used cars")
        self.assertEqual(
            self.live_override().config["optionsRef"]["vocabulary"], VOCABULARY
        )

    def test_the_refusal_is_offered_again_on_the_next_run(self):
        """The sidecar is not allowed to absorb a decision nobody made."""
        self.edit_entry(config=_ref(OTHER, "Make"))
        self.load()

        again = self.load(dry_run=True)

        self.assertEqual(len(again.feature_identity_changes), 1)
        self.assertEqual(again.feature_identity_changes[0].key, "used/make")

    def test_a_description_change_on_the_same_vocabulary_is_a_plain_update(self):
        self.edit_entry(description="feature.make.help")

        report = self.load()

        self.assertFalse(report.failed, [(i.kind, i.key, i.detail) for i in report.categories])
        self.assertEqual(report.feature_identity_changes, [])
        self.assertEqual(self.live_override().description, "feature.make.help")

    def test_the_command_names_the_refused_override(self):
        self.edit_entry(config=_ref(OTHER, "Make"))

        buf = io.StringIO()
        with self.assertRaises(CommandError):
            call_command(
                "load_catalog", dir=self.directory,
                on_conflict=cl.ON_CONFLICT_FIXTURE, stdout=buf,
            )

        text = buf.getvalue()
        self.assertIn("feature identity changes REFUSED: 1", text)
        self.assertIn(
            "used/make: vocabulary 'phones' → 'cars', level 'Model' → 'Make'", text
        )

    def test_the_escape_applies_the_override_swap(self):
        self.edit_entry(config=_ref(OTHER, "Make"))

        report = self.load(allow_feature_identity_change=True)

        self.assertFalse(report.failed, [(i.kind, i.key, i.detail) for i in report.categories])
        self.assertTrue(report.feature_identity_changes[0].applied)
        live = self.live_override()
        self.assertEqual(live.config["optionsRef"]["vocabulary"], OTHER)
        self.assertEqual(live.config["optionsRef"]["level"], "Make")
