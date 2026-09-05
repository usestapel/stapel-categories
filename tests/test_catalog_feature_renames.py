"""``load_catalog`` and feature-slug renames — the half that lives elsewhere.

The incident, 2026-09-05. ``load_catalog --on-conflict fixture-wins`` applied a
fixture in which five car features had new slugs (``make_ref_select`` → ``make``
and four more). The dry run said ``features: updated 62`` and nothing about a
rename. Afterwards the make facet was empty, the search projection had lost the
values, and ``listings_reproject_features`` — which keys on the CURRENT slugs —
would have DROPPED them rather than repaired them: a feature slug is the key
every listing files its answer under, so renaming one here and nowhere else
strands every stored answer at once.

The loader renamed silently. These tests hold the three things that replaced
that silence: renames are named in the plan, a plain apply refuses them, and
``--rename-features`` performs them together with the other half of the
migration — refusing even then when there is nothing to perform that half with.
"""
import io
import tempfile

from django.core.management import call_command
from django.test import override_settings

from stapel_categories import catalog_fixtures as cf
from stapel_categories import catalog_load as cl
from stapel_categories.models import Feature

from .test_catalog_load import _CatalogTestCase, _export, _read_json, _write_json


def _rename_in_fixture(directory, old: str, new: str) -> None:
    """Rename a feature slug in BOTH fixture files, as a source-side rename does.

    The feature's own record moves to the new slug and every category entry
    referencing it follows. Nothing else changes — in particular the NAME does
    not, which is exactly what makes the two records the same feature.
    """
    features = _read_json(directory, cf.FEATURES_FILE)
    for record in features:
        if record.get("slug") == old:
            record["slug"] = new
    _write_json(directory, cf.FEATURES_FILE, features)

    categories = _read_json(directory, cf.CATEGORIES_FILE)
    for record in categories:
        for entry in record.get("features") or ():
            if isinstance(entry, dict) and entry.get("slug") == old:
                entry["slug"] = new
    _write_json(directory, cf.CATEGORIES_FILE, categories)


def _slugs():
    return set(
        Feature.objects.filter(deleted=False).values_list("slug", flat=True)
    )


class _RenameCase(_CatalogTestCase):
    """A seeded catalogue exported, then its ``size`` feature renamed."""

    def setUp(self):
        super().setUp()
        self.seed_catalog()
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.directory = self._dir.name
        _export(self.directory)
        _rename_in_fixture(self.directory, "size", "dimension")

    def load(self, **kwargs):
        return cl.load_catalog(self.directory, **kwargs)


class DetectionTests(_RenameCase):
    def test_the_dry_run_names_the_rename_it_used_to_call_an_update(self):
        report = self.load(dry_run=True)

        self.assertEqual(report.feature_renames, {"size": "dimension"})
        self.assertIn("electronics", report.feature_renames_by_category)
        self.assertEqual(
            report.feature_renames_by_category["electronics"], {"size": "dimension"}
        )
        self.assertFalse(report.feature_renames_applied)

    def test_the_command_prints_the_line_the_incident_never_printed(self):
        out = _run_command(self.directory, dry_run=True)

        self.assertIn("feature renames: 1", out)
        self.assertIn("size → dimension", out)
        self.assertIn("NOT applied", out)
        self.assertIn("--rename-features", out)

    def test_a_genuinely_new_feature_is_not_a_rename(self):
        features = _read_json(self.directory, cf.FEATURES_FILE)
        for record in features:
            if record.get("slug") == "dimension":
                record["name"] = "Something else entirely"
        _write_json(self.directory, cf.FEATURES_FILE, features)

        report = self.load(dry_run=True)

        self.assertEqual(report.feature_renames, {})

    def test_two_features_sharing_one_identity_are_refused_not_guessed(self):
        """A wrong rename writes sellers' answers into the wrong field."""
        features = _read_json(self.directory, cf.FEATURES_FILE)
        features.append({
            **next(r for r in features if r["slug"] == "dimension"),
            "slug": "dimension_two",
        })
        _write_json(self.directory, cf.FEATURES_FILE, features)
        categories = _read_json(self.directory, cf.CATEGORIES_FILE)
        for record in categories:
            if record["slug"] == "electronics":
                record["features"].append({"slug": "dimension_two"})
        _write_json(self.directory, cf.CATEGORIES_FILE, categories)

        report = self.load(dry_run=True)

        self.assertEqual(report.feature_renames, {})
        self.assertTrue(
            any(it.kind == cl.RENAME_BLOCKED for it in report.features),
            [it.detail for it in report.features],
        )


class BlockedApplyTests(_RenameCase):
    def test_a_plain_apply_keeps_the_live_slug(self):
        report = self.load(on_conflict=cl.ON_CONFLICT_FIXTURE)

        self.assertFalse(report.feature_renames_applied)
        self.assertIn("size", _slugs())
        self.assertNotIn("dimension", _slugs())

    def test_the_categories_keep_the_feature_they_had(self):
        """Blocking the feature records alone would leave every category
        referencing a slug no root defines — a refusal turning into a wall of
        dangling-reference errors."""
        self.load(on_conflict=cl.ON_CONFLICT_FIXTURE)

        linked = set(
            self.electronics.category_features.values_list("feature__slug", flat=True)
        )
        self.assertIn("size", linked)
        self.assertEqual(report_errors(self.load(dry_run=True)), [])

    def test_it_is_reported_as_blocked_every_run_not_once(self):
        self.load(on_conflict=cl.ON_CONFLICT_FIXTURE)
        report = self.load(on_conflict=cl.ON_CONFLICT_FIXTURE)

        self.assertEqual(report.feature_renames, {"size": "dimension"})
        blocked = [it for it in report.features if it.kind == cl.RENAME_BLOCKED]
        self.assertTrue(blocked, [it.kind for it in report.features])

    def test_the_rest_of_the_load_still_applies(self):
        categories = _read_json(self.directory, cf.CATEGORIES_FILE)
        for record in categories:
            if record["slug"] == "apparel":
                record["name"] = "Clothing"
        _write_json(self.directory, cf.CATEGORIES_FILE, categories)

        self.load(on_conflict=cl.ON_CONFLICT_FIXTURE)

        from stapel_categories.models import Category
        self.assertEqual(Category.objects.get(slug="apparel").name, "Clothing")


class HookTests(_RenameCase):
    def setUp(self):
        super().setUp()
        self.calls = []
        from stapel_core.comm import register_function
        from stapel_core.comm.registry import function_registry

        def hook(payload):
            self.calls.append(payload)
            return {
                "listings_scanned": 3, "listings_changed": 2,
                "keys_renamed": 2, "conflicts": [],
            }

        register_function("test.rename_hook", hook)
        self.addCleanup(function_registry._providers.pop, "test.rename_hook", None)
        self.addCleanup(function_registry._schemas.pop, "test.rename_hook", None)

    @override_settings(STAPEL_CATEGORIES={"FEATURE_RENAME_HOOK": "test.rename_hook"})
    def test_the_flag_renames_and_hands_the_answers_over(self):
        report = self.load(
            on_conflict=cl.ON_CONFLICT_FIXTURE, rename_features=True
        )

        self.assertTrue(report.feature_renames_applied)
        self.assertIn("dimension", _slugs())
        self.assertEqual(report.rename_hook, "test.rename_hook")
        self.assertEqual(
            [(c["category_id"] is not None, c["renames"]) for c in self.calls],
            [(True, {"size": "dimension"})] * len(self.calls),
        )
        self.assertEqual(
            sorted(entry["category"] for entry in report.rename_hook_results),
            ["electronics", "phones"],
        )

    @override_settings(STAPEL_CATEGORIES={"FEATURE_RENAME_HOOK": "test.rename_hook"})
    def test_the_command_prints_the_counts_the_other_half_reported(self):
        out = _run_command(
            self.directory, on_conflict="fixture-wins", rename_features=True
        )

        self.assertIn("applied", out)
        self.assertIn("test.rename_hook", out)
        self.assertIn("2 key(s) moved across 2 listing(s)", out)

    @override_settings(STAPEL_CATEGORIES={"FEATURE_RENAME_HOOK": "no.such.function"})
    def test_a_failing_hook_is_reported_with_the_map_to_replay(self):
        """The catalogue is already committed; the operator needs the call."""
        report = self.load(
            on_conflict=cl.ON_CONFLICT_FIXTURE, rename_features=True
        )

        self.assertTrue(report.feature_renames_applied)
        self.assertTrue(all("error" in e for e in report.rename_hook_results))
        self.assertEqual(
            report.rename_hook_results[0]["renames"], {"size": "dimension"}
        )


class NoHookTests(_RenameCase):
    @override_settings(STAPEL_CATEGORIES={"FEATURE_RENAME_HOOK": "none"})
    def test_no_hook_configured_refuses_the_rename_rather_than_stranding(self):
        """A flag is not an alibi: with nothing to move the answers, renaming
        the slug IS the incident."""
        report = self.load(
            on_conflict=cl.ON_CONFLICT_FIXTURE, rename_features=True
        )

        self.assertFalse(report.feature_renames_applied)
        self.assertIn("size", _slugs())
        self.assertNotIn("dimension", _slugs())

    @override_settings(STAPEL_CATEGORIES={"FEATURE_RENAME_HOOK": "none"})
    def test_and_says_why_naming_both_ways_out(self):
        out = _run_command(
            self.directory, on_conflict="fixture-wins", rename_features=True
        )

        self.assertIn("FEATURE_RENAME_HOOK", out)
        self.assertIn("--no-hook", out)

    @override_settings(STAPEL_CATEGORIES={"FEATURE_RENAME_HOOK": "none"})
    def test_explicit_no_hook_renames_anyway_and_says_the_answers_did_not_move(self):
        out = _run_command(
            self.directory, on_conflict="fixture-wins",
            rename_features=True, no_hook=True,
        )

        self.assertIn("dimension", _slugs())
        self.assertNotIn("size", _slugs())
        self.assertIn("no rename hook was called", out)


def report_errors(report):
    return [it.detail for it in report.features + report.categories if it.kind == cl.ERROR]


def _run_command(directory, **kwargs) -> str:
    buffer = io.StringIO()
    try:
        call_command("load_catalog", dir=directory, stdout=buffer, **kwargs)
    except Exception:  # a conflicting record exits non-zero; the report still printed
        pass
    return buffer.getvalue()

