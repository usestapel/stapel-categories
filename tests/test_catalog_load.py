"""Tests for ``load_catalog`` / 3-way catalog reconciliation (CAT-2).

Covers docs/catalog-fixtures-sync.md §3-§4 and §6:
- round-trip: export -> load into a clean DB -> export is byte-identical
- idempotency: a second load is zero saves / zero revision bumps / zero events
- 3-way classification: fast-forward, conflict (both sides), db-only drift,
  fixture-only create, deletion (soft default / hard / ignore), delete-conflict
- conflict policies: abort (default, per-record), fixture-wins, db-wins
- is_test untouchability (never updated, never deleted, never clobbered)
- subtree lock is taken; save()-path emits category.changed (outbox atomicity)
- sidecar reflects the applied state after a load
"""
import json
import os
import tempfile

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from treenode.memory import clear_refs

from stapel_categories import catalog_fixtures as cf
from stapel_categories import catalog_load as cl
from stapel_categories.models import Category, CategoryFeature, Feature


def _export(out_dir, **kwargs):
    call_command("export_catalog", out=out_dir, **kwargs)


def _load_cmd(directory, **kwargs):
    call_command("load_catalog", dir=directory, **kwargs)


def _read(out_dir, name):
    with open(os.path.join(out_dir, name), encoding="utf-8") as fh:
        return fh.read()


def _read_json(out_dir, name):
    return json.loads(_read(out_dir, name))


def _write_json(out_dir, name, payload):
    with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
        fh.write(cf.canonical_json(payload))


def _wipe_db():
    """Hard-empty the catalog tables (simulate a clean DB for round-trips)."""
    CategoryFeature.objects.all().delete()
    Category.objects.all().delete()
    Feature.objects.all().delete()
    cache.clear()
    clear_refs(Category)
    clear_refs(Feature)


def _opts(*values):
    """Valid select options (stapel-attributes SelectOption objects)."""
    return [{"value": v, "label": v} for v in values]


def _capture_events():
    """Collect category.changed payloads emitted from now on."""
    from stapel_core.comm import subscribe_action

    received = []
    subscribe_action("category.changed", lambda event: received.append(event.payload))
    return received


class _CatalogTestCase(TestCase):
    def seed_catalog(self):
        """Tree with a shared root feature, an override, and a plain feature."""
        self.color = Feature.objects.create(
            name="Color", slug="color",
            config={"type": "select", "options": _opts("red", "blue")},
            show_as_badge=True,
        )
        self.size = Feature.objects.create(
            name="Size", slug="size", config={"type": "int", "min": 0},
        )
        self.electronics = Category.objects.create(name="Electronics", slug="electronics")
        CategoryFeature.objects.create(category=self.electronics, feature=self.color, order=0)
        CategoryFeature.objects.create(category=self.electronics, feature=self.size, order=1)
        # Child inherits (copy_parent_features), then overrides color.
        self.phones = Category.objects.create(
            name="Phones", slug="phones", tn_parent=self.electronics,
        )
        self.override = Feature.objects.create(
            tn_parent=self.color, name="Color", slug="color",
            config={"type": "select", "options": _opts("red", "green", "blue")},
            mandatory=True,
        )
        link = self.phones.category_features.get(feature=self.color)
        link.feature = self.override
        link.save()
        # An unrelated sibling root category.
        self.apparel = Category.objects.create(name="Apparel", slug="apparel")


# ---------------------------------------------------------------------------
# Round-trip + idempotency (§6 invariants 2 & 3)
# ---------------------------------------------------------------------------


class RoundTripTests(_CatalogTestCase):
    def test_export_load_into_clean_db_export_is_byte_identical(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            _export(first)
            feats_before = _read(first, cf.FEATURES_FILE)
            cats_before = _read(first, cf.CATEGORIES_FILE)
            state_before = _read_json(first, cf.STATE_FILE)

            _wipe_db()
            _load_cmd(first, seed_if_empty=True)

            _export(second)
            self.assertEqual(_read(second, cf.FEATURES_FILE), feats_before)
            self.assertEqual(_read(second, cf.CATEGORIES_FILE), cats_before)
            # Content hashes (per natural key) survive the round trip too;
            # max_revision is DB-local and legitimately differs.
            state_after = _read_json(second, cf.STATE_FILE)
            self.assertEqual(state_after["features"], state_before["features"])
            self.assertEqual(state_after["categories"], state_before["categories"])

    def test_round_trip_preserves_override_structure(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            _wipe_db()
            _load_cmd(out, seed_if_empty=True)

            # The child's link points at an override row (tn_parent set) with
            # the inline config; the parent keeps the shared root.
            phones = Category.objects.get(slug="phones")
            link_feats = [
                link.feature for link in
                phones.category_features.order_by("order", "id").select_related("feature")
            ]
            override = next(f for f in link_feats if (f.slug or "") == "color")
            self.assertIsNotNone(override.tn_parent_id)
            self.assertEqual(override.config["options"], _opts("red", "green", "blue"))
            self.assertTrue(override.mandatory)
            electronics = Category.objects.get(slug="electronics")
            root_link = electronics.category_features.get(feature__slug="color")
            self.assertIsNone(root_link.feature.tn_parent_id)

    def test_load_is_idempotent_zero_saves_zero_events(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            revisions_before = dict(Category.objects.values_list("slug", "revision"))
            feat_revisions_before = list(Feature.objects.values_list("pk", "revision"))
            received = _capture_events()

            report = cl.load_catalog(out)

            self.assertFalse(report.failed)
            self.assertEqual(report.count(cl.SKIPPED), len(report.features) + len(report.categories))
            self.assertEqual(received, [])  # zero category.changed
            self.assertEqual(dict(Category.objects.values_list("slug", "revision")), revisions_before)
            self.assertEqual(list(Feature.objects.values_list("pk", "revision")), feat_revisions_before)

    def test_second_load_after_fast_forward_is_noop(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            next(c for c in cats if c["slug"] == "apparel")["name"] = "Clothing"
            _write_json(out, cf.CATEGORIES_FILE, cats)

            first = cl.load_catalog(out)
            self.assertEqual(first.count(cl.UPDATED), 1)
            self.assertFalse(first.failed)

            received = _capture_events()
            second = cl.load_catalog(out)
            self.assertEqual(second.count(cl.UPDATED), 0)
            self.assertEqual(second.count(cl.CREATED), 0)
            self.assertEqual(received, [])


# ---------------------------------------------------------------------------
# 3-way classification and policies (§4)
# ---------------------------------------------------------------------------


class FastForwardTests(_CatalogTestCase):
    def test_fixture_change_applies_and_bumps_revision(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            rec = next(c for c in cats if c["slug"] == "apparel")
            rec["name"] = "Clothing"
            rec["carousel_enabled"] = True
            _write_json(out, cf.CATEGORIES_FILE, cats)

            old_revision = Category.objects.get(slug="apparel").revision
            received = _capture_events()
            report = cl.load_catalog(out)

            self.assertFalse(report.failed)
            apparel = Category.objects.get(slug="apparel")
            self.assertEqual(apparel.name, "Clothing")
            self.assertTrue(apparel.carousel_enabled)
            self.assertGreater(apparel.revision, old_revision)
            # save()-path emitted the invalidation event (outbox contract).
            self.assertTrue(any(p["category_id"] == apparel.pk for p in received))

    def test_feature_fast_forward_applies(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            feats = _read_json(out, cf.FEATURES_FILE)
            next(f for f in feats if f["slug"] == "size")["name"] = "Dimensions"
            _write_json(out, cf.FEATURES_FILE, feats)

            report = cl.load_catalog(out)
            self.assertFalse(report.failed)
            self.assertEqual(
                Feature.objects.get(slug="size", tn_parent__isnull=True).name,
                "Dimensions",
            )

    def test_new_fixture_record_is_created(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            cats.append({
                "slug": "toys", "parent_slug": None, "name": "Toys",
                "comment": "", "catalog_icon": "", "carousel_icon": "",
                "carousel_enabled": False, "active": True, "translatable": True,
                "features": [{"slug": "color"}],
            })
            _write_json(out, cf.CATEGORIES_FILE, cats)

            report = cl.load_catalog(out)
            self.assertFalse(report.failed)
            toys = Category.objects.get(slug="toys")
            self.assertEqual(
                [link.feature.slug for link in toys.category_features.all()],
                ["color"],
            )
            self.assertEqual(report.count(cl.CREATED), 1)

    def test_new_child_category_created_under_existing_parent(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            cats.append({
                "slug": "laptops", "parent_slug": "electronics", "name": "Laptops",
                "features": [{"slug": "color"}, {"slug": "size"}],
            })
            _write_json(out, cf.CATEGORIES_FILE, cats)

            report = cl.load_catalog(out)
            self.assertFalse(report.failed)
            laptops = Category.objects.get(slug="laptops")
            self.assertEqual(laptops.tn_parent.slug, "electronics")


class ConflictTests(_CatalogTestCase):
    def _diverge_apparel(self, out):
        """Both sides change apparel's name after the export."""
        cats = _read_json(out, cf.CATEGORIES_FILE)
        next(c for c in cats if c["slug"] == "apparel")["name"] = "Fixture Name"
        _write_json(out, cf.CATEGORIES_FILE, cats)
        apparel = Category.objects.get(slug="apparel")
        apparel.name = "DB Name"
        apparel.save()

    def test_both_sides_changed_default_aborts_record_keeps_db(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            self._diverge_apparel(out)

            report = cl.load_catalog(out)
            self.assertTrue(report.failed)
            self.assertEqual(report.conflicts, 1)
            # DB untouched — no silent data loss.
            self.assertEqual(Category.objects.get(slug="apparel").name, "DB Name")

    def test_conflict_does_not_block_other_records(self):
        """Per-record abort: non-conflicting records still apply (§4)."""
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            self._diverge_apparel(out)
            # A second, clean fixture-side edit on another record.
            cats = _read_json(out, cf.CATEGORIES_FILE)
            next(c for c in cats if c["slug"] == "electronics")["comment"] = "updated"
            _write_json(out, cf.CATEGORIES_FILE, cats)

            report = cl.load_catalog(out)
            self.assertTrue(report.failed)  # the conflict still fails the run
            self.assertEqual(Category.objects.get(slug="electronics").comment, "updated")

    def test_fixture_wins_resolves_all_conflicts_to_fixture(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            self._diverge_apparel(out)

            report = cl.load_catalog(out, on_conflict=cl.ON_CONFLICT_FIXTURE)
            self.assertFalse(report.failed)
            self.assertEqual(Category.objects.get(slug="apparel").name, "Fixture Name")

    def test_db_wins_keeps_db_silently(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            self._diverge_apparel(out)

            report = cl.load_catalog(out, on_conflict=cl.ON_CONFLICT_DB)
            self.assertFalse(report.failed)
            self.assertEqual(Category.objects.get(slug="apparel").name, "DB Name")

    def test_removed_from_fixture_plus_local_edit_is_conflict(self):
        """Deletion in fixture vs concurrent DB edit = conflict, not a delete."""
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            cats = [c for c in cats if c["slug"] != "apparel"]
            _write_json(out, cf.CATEGORIES_FILE, cats)
            apparel = Category.objects.get(slug="apparel")
            apparel.name = "Locally Edited"
            apparel.save()

            report = cl.load_catalog(out)
            self.assertTrue(report.failed)
            self.assertEqual(report.conflicts, 1)
            apparel.refresh_from_db()
            self.assertFalse(apparel.deleted)  # NOT deleted by default
            self.assertEqual(apparel.name, "Locally Edited")

            # fixture-wins resolves the delete-conflict as a (soft) delete.
            report = cl.load_catalog(out, on_conflict=cl.ON_CONFLICT_FIXTURE)
            self.assertFalse(report.failed)
            apparel.refresh_from_db()
            self.assertTrue(apparel.deleted)

    def test_db_only_drift_warns_and_keeps_db(self):
        """Changed in DB, untouched in fixture -> warn, don't revert (§4)."""
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            apparel = Category.objects.get(slug="apparel")
            apparel.name = "Admin Edited"
            apparel.save()

            report = cl.load_catalog(out)
            self.assertFalse(report.failed)  # a warning, not a failure
            self.assertEqual(report.count(cl.DB_ONLY), 1)
            self.assertEqual(Category.objects.get(slug="apparel").name, "Admin Edited")

            # The drift stays visible on the next run (base not advanced).
            report2 = cl.load_catalog(out)
            self.assertEqual(report2.count(cl.DB_ONLY), 1)

    def test_db_new_record_is_left_alone_and_noted(self):
        """Created in DB after the export, never in canon -> untouched."""
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            Category.objects.create(name="Admin Made", slug="admin-made")

            report = cl.load_catalog(out)
            self.assertFalse(report.failed)
            self.assertEqual(report.count(cl.DB_NEW), 1)
            self.assertTrue(Category.objects.filter(slug="admin-made", deleted=False).exists())


class DeletionTests(_CatalogTestCase):
    def _drop_apparel(self, out):
        cats = [c for c in _read_json(out, cf.CATEGORIES_FILE) if c["slug"] != "apparel"]
        _write_json(out, cf.CATEGORIES_FILE, cats)

    def test_default_deletion_is_soft(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            self._drop_apparel(out)

            report = cl.load_catalog(out)
            self.assertFalse(report.failed)
            self.assertEqual(report.count(cl.DELETED), 1)
            apparel = Category.objects.get(slug="apparel")
            self.assertTrue(apparel.deleted)  # soft: row remains, reversible

    def test_hard_deletion_removes_the_row(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            self._drop_apparel(out)

            cl.load_catalog(out, deletions=cl.DELETIONS_HARD)
            self.assertFalse(Category.objects.filter(slug="apparel").exists())

    def test_deletions_ignore_never_deletes(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            self._drop_apparel(out)

            report = cl.load_catalog(out, deletions=cl.DELETIONS_IGNORE)
            self.assertFalse(report.failed)
            self.assertEqual(report.count(cl.DELETED), 0)
            self.assertFalse(Category.objects.get(slug="apparel").deleted)

    def test_feature_removed_from_fixture_is_soft_deleted(self):
        self.seed_catalog()
        # Detach size from electronics so the deletion is clean.
        CategoryFeature.objects.filter(feature=self.size).delete()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            feats = [f for f in _read_json(out, cf.FEATURES_FILE) if f["slug"] != "size"]
            _write_json(out, cf.FEATURES_FILE, feats)

            report = cl.load_catalog(out)
            self.assertFalse(report.failed)
            self.size.refresh_from_db()
            self.assertTrue(self.size.deleted)

    def test_soft_deleted_row_resurrects_on_fixture_create(self):
        """A slug re-added to the fixture revives its soft-deleted row."""
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            self._drop_apparel(out)
            cl.load_catalog(out)  # soft-deletes apparel
            self.assertTrue(Category.objects.get(slug="apparel").deleted)

            # Re-add it to the fixture (as a brand-new record: base was dropped).
            cats = _read_json(out, cf.CATEGORIES_FILE)
            cats.append({"slug": "apparel", "parent_slug": None, "name": "Apparel Again"})
            _write_json(out, cf.CATEGORIES_FILE, cats)

            report = cl.load_catalog(out)
            self.assertFalse(report.failed)
            apparel = Category.objects.get(slug="apparel")
            self.assertFalse(apparel.deleted)
            self.assertEqual(apparel.name, "Apparel Again")


# ---------------------------------------------------------------------------
# is_test untouchability (§5)
# ---------------------------------------------------------------------------


class IsTestTests(_CatalogTestCase):
    def test_is_test_rows_survive_load_and_deletions(self):
        self.seed_catalog()
        scratch_cat = Category.objects.create(name="Scratch", slug="scratch", is_test=True)
        scratch_feat = Feature.objects.create(
            name="Scratch", slug="scratch_feat", config={"type": "string"}, is_test=True,
        )
        with tempfile.TemporaryDirectory() as out:
            _export(out)  # is_test rows are not exported (CAT-1)
            # Even with hard deletions, test rows are invisible to the diff:
            # they are outside the canon by construction.
            report = cl.load_catalog(out, deletions=cl.DELETIONS_HARD)
            self.assertFalse(report.failed)
            scratch_cat.refresh_from_db()
            scratch_feat.refresh_from_db()
            self.assertFalse(scratch_cat.deleted)
            self.assertFalse(scratch_feat.deleted)

    def test_fixture_slug_colliding_with_is_test_row_errors_not_clobbers(self):
        self.seed_catalog()
        Category.objects.create(name="Scratch", slug="colliding", is_test=True)
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            cats.append({"slug": "colliding", "parent_slug": None, "name": "Real"})
            _write_json(out, cf.CATEGORIES_FILE, cats)

            report = cl.load_catalog(out)
            self.assertTrue(report.failed)
            self.assertEqual(report.errors, 1)
            colliding = Category.objects.get(slug="colliding")
            self.assertTrue(colliding.is_test)      # untouched
            self.assertEqual(colliding.name, "Scratch")

    def test_is_test_rows_do_not_appear_in_report_diff(self):
        self.seed_catalog()
        Category.objects.create(name="Scratch", slug="scratch", is_test=True)
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            report = cl.load_catalog(out)
            keys = [it.key for it in report.categories]
            self.assertNotIn("scratch", keys)   # not even a db_new note


# ---------------------------------------------------------------------------
# Overrides: fast-forward and copy-on-write for shared rows
# ---------------------------------------------------------------------------


class OverrideReconcileTests(_CatalogTestCase):
    def test_inline_override_fast_forward_updates_row_in_place(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            phones = next(c for c in cats if c["slug"] == "phones")
            entry = next(e for e in phones["features"] if e["slug"] == "color")
            entry["config"] = {"type": "select", "options": _opts("black", "white")}
            _write_json(out, cf.CATEGORIES_FILE, cats)

            rows_before = Feature.objects.count()
            report = cl.load_catalog(out)
            self.assertFalse(report.failed)
            self.override.refresh_from_db()
            self.assertEqual(self.override.config["options"], _opts("black", "white"))
            self.assertEqual(Feature.objects.count(), rows_before)  # in place, no new row

    def test_shared_override_row_is_copied_not_mutated(self):
        """A row linked by several categories is copy-on-write (H-risk).

        Repoint the parent's link at the same override row the child uses
        (inherit-propagation shape), then change only the child's inline
        config in the fixture: the parent's schema must NOT change.
        """
        self.seed_catalog()
        parent_link = self.electronics.category_features.get(feature=self.color)
        parent_link.feature = self.override
        parent_link.save()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            phones = next(c for c in cats if c["slug"] == "phones")
            entry = next(e for e in phones["features"] if e["slug"] == "color")
            entry["config"] = {"type": "select", "options": _opts("only-phones")}
            _write_json(out, cf.CATEGORIES_FILE, cats)

            report = cl.load_catalog(out)
            self.assertFalse(report.failed)
            # Parent still sees the original shared override config.
            self.override.refresh_from_db()
            self.assertEqual(self.override.config["options"], _opts("red", "green", "blue"))
            # Child got its own new row with the new config.
            phones_link = Category.objects.get(slug="phones").category_features.get(
                feature__slug="color"
            )
            self.assertNotEqual(phones_link.feature_id, self.override.pk)
            self.assertEqual(phones_link.feature.config["options"], _opts("only-phones"))


# ---------------------------------------------------------------------------
# Lock / transaction behavior (§6 invariant 5)
# ---------------------------------------------------------------------------


class LockTests(_CatalogTestCase):
    def test_load_takes_select_for_update_lock(self):
        """The load must row-lock the catalog before reconciling (M-5 pattern).

        sqlite ignores FOR UPDATE, so assert at the queryset seam: both models'
        querysets go through select_for_update() inside the load transaction.
        """
        self.seed_catalog()
        from django.db.models.query import QuerySet

        calls = []
        original = QuerySet.select_for_update

        def spy(qs, *args, **kwargs):
            calls.append(qs.model.__name__)
            return original(qs, *args, **kwargs)

        with tempfile.TemporaryDirectory() as out:
            _export(out)
            QuerySet.select_for_update = spy
            try:
                cl.load_catalog(out)
            finally:
                QuerySet.select_for_update = original

        self.assertIn("Category", calls)
        self.assertIn("Feature", calls)

    def test_dry_run_takes_no_lock_and_writes_nothing(self):
        self.seed_catalog()
        from django.db.models.query import QuerySet

        calls = []
        original = QuerySet.select_for_update

        def spy(qs, *args, **kwargs):
            calls.append(qs.model.__name__)
            return original(qs, *args, **kwargs)

        with tempfile.TemporaryDirectory() as out:
            _export(out)
            state_before = _read(out, cf.STATE_FILE)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            next(c for c in cats if c["slug"] == "apparel")["name"] = "Changed"
            _write_json(out, cf.CATEGORIES_FILE, cats)

            QuerySet.select_for_update = spy
            try:
                report = cl.load_catalog(out, dry_run=True)
            finally:
                QuerySet.select_for_update = original

            self.assertEqual(calls, [])
            self.assertTrue(report.dry_run)
            self.assertEqual(report.count(cl.UPDATED), 1)
            # Nothing written: DB untouched, sidecar untouched.
            self.assertEqual(Category.objects.get(slug="apparel").name, "Apparel")
            self.assertEqual(_read(out, cf.STATE_FILE), state_before)


# ---------------------------------------------------------------------------
# Outbox atomicity (emit-check)
# ---------------------------------------------------------------------------


class OutboxAtomicityTests(_CatalogTestCase):
    def test_failing_emit_rolls_back_the_load_and_keeps_sidecar(self):
        """The load writes via save() -> mutate_and_emit: a failing emit must
        sink the surrounding transaction (no half-applied catalog, no advanced
        sidecar) — the same outbox guarantee as an admin edit."""
        from unittest import mock

        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            state_before = _read(out, cf.STATE_FILE)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            next(c for c in cats if c["slug"] == "apparel")["name"] = "New Name"
            _write_json(out, cf.CATEGORIES_FILE, cats)

            def boom(event):
                raise RuntimeError("comm backend down")

            with mock.patch("stapel_core.comm.actions.deliver", boom):
                with self.assertRaises(RuntimeError):
                    cl.load_catalog(out)

            # Mutation rolled back, sidecar not advanced.
            self.assertEqual(Category.objects.get(slug="apparel").name, "Apparel")
            self.assertEqual(_read(out, cf.STATE_FILE), state_before)


# ---------------------------------------------------------------------------
# Sidecar update semantics
# ---------------------------------------------------------------------------


class SidecarUpdateTests(_CatalogTestCase):
    def test_sidecar_reflects_applied_state(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            # fast-forward one record, delete another, leave one conflicted
            cats = _read_json(out, cf.CATEGORIES_FILE)
            next(c for c in cats if c["slug"] == "electronics")["comment"] = "ff"
            cats = [c for c in cats if c["slug"] != "apparel"]
            _write_json(out, cf.CATEGORIES_FILE, cats)
            phones = Category.objects.get(slug="phones")
            phones.name = "DB Phones"
            phones.save()
            fixture_cats = _read_json(out, cf.CATEGORIES_FILE)
            next(c for c in fixture_cats if c["slug"] == "phones")["name"] = "Fixture Phones"
            _write_json(out, cf.CATEGORIES_FILE, fixture_cats)

            state_before = _read_json(out, cf.STATE_FILE)
            report = cl.load_catalog(out)
            self.assertTrue(report.failed)  # phones conflicted
            state_after = _read_json(out, cf.STATE_FILE)

            # fast-forwarded record: base advanced to the new applied DB hash
            _, _, db_state = cf.build_catalog()
            self.assertEqual(
                state_after["categories"]["electronics"],
                db_state["categories"]["electronics"],
            )
            self.assertNotEqual(
                state_after["categories"]["electronics"],
                state_before["categories"]["electronics"],
            )
            # deleted record: dropped from the base
            self.assertNotIn("apparel", state_after["categories"])
            # conflicted record: base kept at the OLD hash (still flagged next run)
            self.assertEqual(
                state_after["categories"]["phones"],
                state_before["categories"]["phones"],
            )
            self.assertEqual(state_after["version"], cf.STATE_VERSION)

    def test_incompatible_sidecar_version_refuses_to_run(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            state = _read_json(out, cf.STATE_FILE)
            state["version"] = 999
            _write_json(out, cf.STATE_FILE, state)
            with self.assertRaises(ValueError):
                cl.load_catalog(out)

    def test_missing_sidecar_treats_fixture_as_creates_or_conflicts(self):
        """No base: identical records skip, diverged ones conflict (no silent wins)."""
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            os.remove(os.path.join(out, cf.STATE_FILE))

            report = cl.load_catalog(out)
            self.assertFalse(report.failed)  # everything matches -> converged/skip
            # A successful load regenerates the sidecar from the applied state.
            self.assertTrue(os.path.exists(os.path.join(out, cf.STATE_FILE)))

            apparel = Category.objects.get(slug="apparel")
            apparel.name = "Drifted"
            apparel.save()
            os.remove(os.path.join(out, cf.STATE_FILE))  # again: no base at all
            report = cl.load_catalog(out)
            self.assertTrue(report.failed)  # fixture != db with no base = conflict
            self.assertEqual(Category.objects.get(slug="apparel").name, "Drifted")


# ---------------------------------------------------------------------------
# seed-if-empty + command-level behavior
# ---------------------------------------------------------------------------


class SeedIfEmptyTests(_CatalogTestCase):
    def test_seed_if_empty_on_populated_db_is_noop(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            name_before = Category.objects.get(slug="apparel").name
            # Populate-side drift that a real load would touch:
            cats = _read_json(out, cf.CATEGORIES_FILE)
            next(c for c in cats if c["slug"] == "apparel")["name"] = "Changed"
            _write_json(out, cf.CATEGORIES_FILE, cats)

            report = cl.load_catalog(out, seed_if_empty=True)
            self.assertFalse(report.failed)
            self.assertEqual(Category.objects.get(slug="apparel").name, name_before)

    def test_seed_if_empty_on_empty_db_loads_everything(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            _wipe_db()
            report = cl.load_catalog(out, seed_if_empty=True)
            self.assertFalse(report.failed)
            self.assertEqual(
                set(Category.objects.values_list("slug", flat=True)),
                {"electronics", "phones", "apparel"},
            )


class CommandTests(_CatalogTestCase):
    def test_command_raises_on_conflict_for_nonzero_exit(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            next(c for c in cats if c["slug"] == "apparel")["name"] = "Fixture"
            _write_json(out, cf.CATEGORIES_FILE, cats)
            apparel = Category.objects.get(slug="apparel")
            apparel.name = "DB"
            apparel.save()

            with self.assertRaises(CommandError):
                _load_cmd(out)

    def test_command_errors_when_fixtures_missing(self):
        with tempfile.TemporaryDirectory() as out:
            with self.assertRaises(CommandError):
                _load_cmd(out)

    def test_dry_run_reports_all_classes(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            # create one of each class
            cats = _read_json(out, cf.CATEGORIES_FILE)
            next(c for c in cats if c["slug"] == "electronics")["comment"] = "ff"   # fast-forward
            cats = [c for c in cats if c["slug"] != "apparel"]                       # deletion
            cats.append({"slug": "toys", "parent_slug": None, "name": "Toys"})      # create
            _write_json(out, cf.CATEGORIES_FILE, cats)
            phones = Category.objects.get(slug="phones")
            phones.name = "DB Phones"                                                # db-only
            phones.save()
            Category.objects.create(name="Admin Made", slug="admin-made")           # db-new

            report = cl.load_catalog(out, dry_run=True)
            kinds = {it.key: it.kind for it in report.categories}
            self.assertEqual(kinds["electronics"], cl.UPDATED)
            self.assertEqual(kinds["apparel"], cl.DELETED)
            self.assertEqual(kinds["toys"], cl.CREATED)
            self.assertEqual(kinds["phones"], cl.DB_ONLY)
            self.assertEqual(kinds["admin-made"], cl.DB_NEW)
            # dry run mutated nothing
            self.assertFalse(Category.objects.filter(slug="toys").exists())
            self.assertFalse(Category.objects.get(slug="apparel").deleted)

    def test_dangling_feature_reference_is_per_record_error(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            cats.append({
                "slug": "broken", "parent_slug": None, "name": "Broken",
                "features": [{"slug": "no-such-feature"}],
            })
            cats.append({"slug": "fine", "parent_slug": None, "name": "Fine"})
            _write_json(out, cf.CATEGORIES_FILE, cats)

            report = cl.load_catalog(out)
            self.assertTrue(report.failed)
            self.assertEqual(report.errors, 1)
            # The broken record did not poison the rest of the run.
            self.assertTrue(Category.objects.filter(slug="fine").exists())
            self.assertFalse(Category.objects.filter(slug="broken").exists())

    def test_hand_written_fixture_with_defaults_omitted_is_stable(self):
        """Sparse hand-written records normalize to canonical form: applying
        them once converges (second load = zero events, no revision churn)."""
        with tempfile.TemporaryDirectory() as out:
            _write_json(out, cf.FEATURES_FILE, [
                {"slug": "color", "name": "Color", "config": {"type": "string"}},
            ])
            _write_json(out, cf.CATEGORIES_FILE, [
                {"slug": "root-cat", "name": "Root", "features": [{"slug": "color"}]},
            ])

            report = cl.load_catalog(out)
            self.assertFalse(report.failed)
            self.assertTrue(Category.objects.filter(slug="root-cat").exists())

            received = _capture_events()
            second = cl.load_catalog(out)
            self.assertFalse(second.failed)
            self.assertEqual(second.count(cl.CREATED) + second.count(cl.UPDATED), 0)
            self.assertEqual(received, [])
