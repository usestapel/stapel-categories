"""Presentation fields are stand-owned; a re-import must not clobber them.

The catalogue fixture's contract is TAXONOMY + FEATURES (which categories
exist, where they sit, what they type). ``catalog_icon`` / ``carousel_icon``
/ ``carousel_enabled`` are the *operator's*: on a live classified stand
somebody curated ten roots onto the home-screen carousel, and the next
catalogue re-import — a fixture-side rename under ``--on-conflict
fixture-wins`` — reset all three to their defaults ("", "", False) and the
home screen lost its tiles. ``tn_priority`` was already fixture-invisible;
these three were not, for no reason the contract could defend.

Mechanism under test (0.13.0):

* the three presentation keys are excluded from BOTH sides of the 3-way
  content hash (``cf.category_sync_view``), so a presentation-only change on
  either side is not a sync event at all — no fast-forward, no db-only drift
  warning, no phantom conflict;
* ``_apply_category_upsert`` writes them only when it CREATES the row (an
  export→restore of a whole stand keeps its curation); on an update it
  leaves whatever the row has.
"""
import tempfile

from stapel_categories import catalog_fixtures as cf
from stapel_categories import catalog_load as cl
from stapel_categories.models import Category

from .test_catalog_load import (
    _CatalogTestCase,
    _export,
    _read_json,
    _wipe_db,
    _write_json,
)


def _curate(slug="apparel", **extra):
    """The operator's home-screen edit: enable + icon + ordering."""
    cat = Category.objects.get(slug=slug)
    cat.carousel_enabled = True
    cat.carousel_icon = "x"
    cat.tn_priority = 5
    for key, value in extra.items():
        setattr(cat, key, value)
    cat.save()
    return cat


class PresentationSurvivesUpdateTests(_CatalogTestCase):
    def test_fixture_rename_updates_name_and_keeps_curation(self):
        """The stand's exact sequence: curate, then re-import a renamed node."""
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            _curate()

            cats = _read_json(out, cf.CATEGORIES_FILE)
            rec = next(c for c in cats if c["slug"] == "apparel")
            rec["name"] = "Clothing"
            _write_json(out, cf.CATEGORIES_FILE, cats)

            report = cl.load_catalog(out)
            self.assertFalse(report.failed)

            apparel = Category.objects.get(slug="apparel")
            self.assertEqual(apparel.name, "Clothing")       # taxonomy: fixture's
            self.assertTrue(apparel.carousel_enabled)        # presentation: operator's
            self.assertEqual(apparel.carousel_icon, "x")
            self.assertEqual(apparel.tn_priority, 5)

    def test_first_load_with_no_sidecar_fixture_wins_keeps_curation(self):
        """No sidecar → every changed record is a conflict; ``fixture-wins``
        resolves the record to the fixture — but presentation is not part of
        the record's sync content, so the curation survives even the most
        aggressive policy. This is the load the stand actually ran."""
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            import os
            os.remove(os.path.join(out, cf.STATE_FILE))
            _curate()

            cats = _read_json(out, cf.CATEGORIES_FILE)
            rec = next(c for c in cats if c["slug"] == "apparel")
            rec["name"] = "Clothing"
            _write_json(out, cf.CATEGORIES_FILE, cats)

            report = cl.load_catalog(out, on_conflict=cl.ON_CONFLICT_FIXTURE)
            self.assertFalse(report.failed)

            apparel = Category.objects.get(slug="apparel")
            self.assertEqual(apparel.name, "Clothing")
            self.assertTrue(apparel.carousel_enabled)
            self.assertEqual(apparel.carousel_icon, "x")
            self.assertEqual(apparel.tn_priority, 5)


class PresentationHashInvisibilityTests(_CatalogTestCase):
    def test_operator_presentation_edit_is_not_db_drift(self):
        """A curation edit must not read as "changed in DB since last export"
        — that warning tells the operator to run export_catalog, and doing so
        for a carousel toggle churns canon with content the fixture does not
        own."""
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            _curate()

            old_revision = Category.objects.get(slug="apparel").revision
            report = cl.load_catalog(out)

            self.assertFalse(report.failed)
            self.assertEqual(report.count(cl.DB_ONLY), 0)
            kinds = {it.key: it.kind for it in report.categories}
            self.assertEqual(kinds.get("apparel"), cl.SKIPPED)
            # And nothing was written: no phantom revision bump.
            self.assertEqual(
                Category.objects.get(slug="apparel").revision, old_revision
            )

    def test_reload_after_curation_is_still_idempotent(self):
        """The dirty guard must not see phantom changes: load, curate, load,
        load — the second and third loads write nothing."""
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            _curate()
            cl.load_catalog(out)

            revisions = dict(Category.objects.values_list("slug", "revision"))
            report = cl.load_catalog(out)
            self.assertFalse(report.failed)
            self.assertEqual(report.count(cl.CREATED), 0)
            self.assertEqual(report.count(cl.UPDATED), 0)
            self.assertEqual(
                dict(Category.objects.values_list("slug", "revision")), revisions
            )

    def test_fixture_side_presentation_change_is_ignored(self):
        """The other direction of the same contract: a fixture that carries a
        different icon (a stale export from another stand, say) does not move
        the row — presentation is simply not the fixture's to say."""
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            rec = next(c for c in cats if c["slug"] == "apparel")
            rec["carousel_enabled"] = True
            rec["carousel_icon"] = "other-stand-icon"
            _write_json(out, cf.CATEGORIES_FILE, cats)

            old_revision = Category.objects.get(slug="apparel").revision
            report = cl.load_catalog(out)

            self.assertFalse(report.failed)
            apparel = Category.objects.get(slug="apparel")
            self.assertFalse(apparel.carousel_enabled)
            self.assertEqual(apparel.carousel_icon, "")
            self.assertEqual(apparel.revision, old_revision)


class PresentationOnCreateTests(_CatalogTestCase):
    def test_restore_into_empty_db_keeps_exported_presentation(self):
        """Export→wipe→load is a stand restore, and the exported files DO
        carry presentation (export's own output is untouched by the hash
        exclusion) — a created row takes it, or a backup would silently
        strip every carousel on restore."""
        self.seed_catalog()
        _curate()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            _wipe_db()

            # The bootstrap idiom: an empty DB ignores the sidecar so every
            # record is a clean create (not a "db deleted it" warning).
            report = cl.load_catalog(out, seed_if_empty=True)
            self.assertFalse(report.failed)

            apparel = Category.objects.get(slug="apparel")
            self.assertTrue(apparel.carousel_enabled)
            self.assertEqual(apparel.carousel_icon, "x")


class ActiveIsCurationTests(_CatalogTestCase):
    """``active`` is the operator's too — a re-import must not RESURRECT.

    The second bite of the same class (0.15.0). An operator deactivated two
    untyped leaves and a duplicate sibling in the admin; the next catalogue
    load — changed records, for real reasons of its own — rewrote them
    wholesale and brought all three back live, because ``active`` was written
    on update while the three presentation keys were not.

    Whether a category is offered to sellers on THIS stand is curation, in
    the same sense ``carousel_enabled`` is. The fixture seeds it on CREATE
    (an export→restore keeps the stand's state); an update never touches it,
    so a deactivation cannot be undone by an import. The producer has no way
    to express retirement through this key anyway — it emits ``active: true``
    for every record — and retiring a category from canon is what
    ``--deletions`` is for.
    """

    def test_a_deactivated_category_is_not_resurrected_by_a_changed_record(self):
        """The stand's exact sequence: deactivate, then re-import a record
        that legitimately changed for an unrelated reason."""
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            _curate(active=False)

            cats = _read_json(out, cf.CATEGORIES_FILE)
            rec = next(c for c in cats if c["slug"] == "apparel")
            self.assertTrue(rec["active"], "the fixture side must claim active")
            rec["name"] = "Clothing"          # a real, unrelated change
            _write_json(out, cf.CATEGORIES_FILE, cats)

            report = cl.load_catalog(out)
            self.assertFalse(report.failed)

            apparel = Category.objects.get(slug="apparel")
            self.assertEqual(apparel.name, "Clothing")   # taxonomy: fixture's
            self.assertFalse(apparel.active)             # curation: operator's

    def test_fixture_wins_with_no_sidecar_still_does_not_resurrect(self):
        """The most aggressive policy there is — every record a conflict,
        resolved to the fixture — still may not re-activate."""
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            import os
            os.remove(os.path.join(out, cf.STATE_FILE))
            _curate(active=False)

            cats = _read_json(out, cf.CATEGORIES_FILE)
            rec = next(c for c in cats if c["slug"] == "apparel")
            rec["name"] = "Clothing"
            _write_json(out, cf.CATEGORIES_FILE, cats)

            report = cl.load_catalog(out, on_conflict=cl.ON_CONFLICT_FIXTURE)
            self.assertFalse(report.failed)
            self.assertFalse(Category.objects.get(slug="apparel").active)

    def test_a_deactivation_is_not_db_drift_and_reload_is_idempotent(self):
        """Same hash contract as presentation: deactivating on the stand is
        not a sync event, so it neither warns as db-drift nor re-writes the
        row on every subsequent load."""
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            _curate(active=False)

            old_revision = Category.objects.get(slug="apparel").revision
            report = cl.load_catalog(out)
            self.assertFalse(report.failed)
            self.assertEqual(report.count(cl.DB_ONLY), 0)
            self.assertEqual(
                {it.key: it.kind for it in report.categories}.get("apparel"),
                cl.SKIPPED,
            )
            self.assertEqual(
                Category.objects.get(slug="apparel").revision, old_revision
            )

            report = cl.load_catalog(out)   # and again: nothing to write
            self.assertEqual(report.count(cl.UPDATED), 0)
            self.assertFalse(Category.objects.get(slug="apparel").active)

    def test_create_still_applies_the_fixtures_active(self):
        """The one direction that stays the fixture's: a row that does not
        exist yet is seeded with what the record says, so an export→restore
        of a whole stand rebuilds its state — inactive rows included."""
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            for rec in cats:
                if rec["slug"] == "apparel":
                    rec["active"] = False
            _write_json(out, cf.CATEGORIES_FILE, cats)

            # A genuine restore into an empty stand: no rows AND no sidecar
            # base (the sidecar describes a sync that never happened here).
            import os
            os.remove(os.path.join(out, cf.STATE_FILE))
            _wipe_db()
            report = cl.load_catalog(out)
            self.assertFalse(report.failed)
            self.assertEqual(
                sum(1 for it in report.categories if it.kind == cl.CREATED), 3
            )
            self.assertFalse(Category.objects.get(slug="apparel").active)
            self.assertTrue(Category.objects.get(slug="electronics").active)
