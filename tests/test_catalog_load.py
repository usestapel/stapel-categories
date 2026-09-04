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
import io
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
            _write_json(out, cf.CATEGORIES_FILE, cats)

            old_revision = Category.objects.get(slug="apparel").revision
            received = _capture_events()
            report = cl.load_catalog(out)

            self.assertFalse(report.failed)
            apparel = Category.objects.get(slug="apparel")
            self.assertEqual(apparel.name, "Clothing")
            # carousel_enabled is deliberately NOT part of this test any
            # more: presentation is stand-owned and fixture-invisible since
            # 0.13.0 — see test_catalog_presentation.py.
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

    def test_fixture_wins_reverts_db_only_drift(self):
        """``fixture-wins`` means the fixture wins an EDIT too, not only a delete.

        The delete-side of db-only drift already resurrects from canon under
        this policy (``_DB_ONLY_DELETION``); leaving the edit-side on "keep
        the DB, warn" made the flag mean two opposite things.
        """
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            apparel = Category.objects.get(slug="apparel")
            apparel.name = "Admin Edited"
            apparel.save()

            report = cl.load_catalog(out, on_conflict=cl.ON_CONFLICT_FIXTURE)
            self.assertFalse(report.failed)
            self.assertEqual(report.count(cl.DB_ONLY), 0)
            self.assertEqual(Category.objects.get(slug="apparel").name, "Apparel")

            # Converged: nothing left to say on the next run.
            report2 = cl.load_catalog(out, on_conflict=cl.ON_CONFLICT_FIXTURE)
            self.assertEqual(report2.count(cl.UPDATED), 0)
            self.assertEqual(report2.count(cl.DB_ONLY), 0)

    def test_fixture_wins_restores_a_root_type_the_db_drifted(self):
        """A drifted ROOT type strands every override the fixture carries for it.

        The stand shape this comes from: two fixture directories share one
        feature-slug namespace, the narrower one was loaded last and left
        ``color`` typed differently from what the wider one's per-category
        overrides say. The root was db-only drift (the wider fixture had not
        changed), so it was never reverted — and every category record whose
        override the fixture DID change was then refused by ``Feature.clean``
        ("Child config.type must match parent config.type"), the same 15
        records on every pass. Reverting the drift is what makes it converge.
        """
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)

            # Someone (here: another fixture directory's load) retypes the root.
            root = Feature.objects.get(slug="color", tn_parent__isnull=True)
            root.config = {"type": "string", "maxLength": 64}
            root.save()

            # …and this directory's fixture edits the override that hangs off it.
            cats = _read_json(out, cf.CATEGORIES_FILE)
            entry = next(e for e in next(c for c in cats if c["slug"] == "phones")["features"]
                         if e.get("slug") == "color")
            entry["description"] = "pick one"
            _write_json(out, cf.CATEGORIES_FILE, cats)

            report = cl.load_catalog(
                out, on_conflict=cl.ON_CONFLICT_FIXTURE, deletions=cl.DELETIONS_IGNORE,
            )
            self.assertEqual(report.count(cl.ERROR), 0)
            self.assertFalse(report.failed)
            self.assertEqual(
                Feature.objects.get(slug="color", tn_parent__isnull=True).config["type"],
                "select",
            )
            override = Feature.objects.get(slug="color", tn_parent__isnull=False)
            self.assertEqual(override.description, "pick one")

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

    def test_orphaned_override_is_soft_deleted_not_left_dangling(self):
        """Removing a category's only use of an override does not leak it (L-finding).

        ``self.override`` is linked only from ``phones``. Dropping it from the
        fixture's feature list removes that link (a normal stale-link
        cleanup); without the fix, the override row itself — reachable from
        nowhere afterward — would sit in the table forever (never surfaced by
        export, never touched by a future load). It must be soft-deleted
        instead, and the round trip stays byte-identical (the row was already
        invisible to export either way).
        """
        self.seed_catalog()
        override_pk = self.override.pk
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            phones = next(c for c in cats if c["slug"] == "phones")
            phones["features"] = [e for e in phones["features"] if e["slug"] != "color"]
            _write_json(out, cf.CATEGORIES_FILE, cats)

            report = cl.load_catalog(out)
            self.assertFalse(report.failed)

            orphan = Feature.objects.get(pk=override_pk)
            self.assertTrue(orphan.deleted)
            self.assertFalse(orphan.feature_categories.exists())

            # No leftover garbage in a re-export, and a second load is still
            # zero writes (the soft-deleted orphan is invisible, not reprocessed).
            with tempfile.TemporaryDirectory() as reexport:
                _export(reexport)
                self.assertEqual(cf.find_orphan_overrides(), [])
            self.assertFalse(cl.load_catalog(out).failed)


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

            # fast-forwarded record: base advanced to the PAIR it synced at —
            # the applied DB hash plus the fixture hash that asked for it.
            _, _, db_state = cf.build_catalog()
            entry = state_after["categories"]["electronics"]
            self.assertEqual(entry["db"], db_state["categories"]["electronics"])
            fix_cat = cl._load_inputs(out)[1]
            self.assertEqual(
                entry["fixture"],
                cf.content_hash(cf.category_sync_view(fix_cat["electronics"])),
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

    def test_seed_if_empty_ignores_is_test_rows(self):
        """A DB holding only is_test scratch data must still read as "empty" (L-finding).

        is_test rows are outside canon by construction (§5) — a test suite
        that seeds is_test fixtures before a bootstrap ``--seed-if-empty``
        call must not have that scratch data mistaken for "already seeded",
        which would silently strand the real canon out of the DB forever.
        """
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            _wipe_db()
            # DB is not truly empty, but only holds is_test scratch rows.
            Feature.objects.create(name="Scratch", slug="scratch", is_test=True,
                                    config={"type": "string"})
            Category.objects.create(name="Scratch Cat", slug="scratch-cat", is_test=True)

            report = cl.load_catalog(out, seed_if_empty=True)
            self.assertFalse(report.failed)
            self.assertEqual(
                set(Category.objects.filter(is_test=False).values_list("slug", flat=True)),
                {"electronics", "phones", "apparel"},
            )
            # The pre-existing is_test rows are untouched, not clobbered.
            self.assertTrue(Category.objects.filter(slug="scratch-cat", is_test=True).exists())
            self.assertTrue(Feature.objects.filter(slug="scratch", is_test=True).exists())


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

    def test_dry_run_predicts_an_override_the_apply_would_refuse(self):
        """A plan is worth nothing if the load it predicts fails anyway.

        An override whose ``config.type`` contradicts the type its root will
        hold after this load is refused by ``Feature.clean`` at write time.
        Under ``--on-conflict abort`` the drifted root is left alone, so the
        refusal is certain — and the dry run has to SAY so, not report a
        clean plan and let the apply discover it.
        """
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            root = Feature.objects.get(slug="color", tn_parent__isnull=True)
            root.config = {"type": "string", "maxLength": 64}
            root.save()
            cats = _read_json(out, cf.CATEGORIES_FILE)
            entry = next(e for e in next(c for c in cats if c["slug"] == "phones")["features"]
                         if e.get("slug") == "color")
            entry["description"] = "pick one"
            _write_json(out, cf.CATEGORIES_FILE, cats)

            report = cl.load_catalog(out, dry_run=True, deletions=cl.DELETIONS_IGNORE)
            self.assertTrue(report.failed)
            item = next(it for it in report.categories
                        if it.key == "phones" and it.kind == cl.ERROR)
            self.assertIn("color", item.detail)
            self.assertIn("select", item.detail)
            self.assertIn("string", item.detail)

            # …and the same plan is clean once the root is reconciled by the
            # policy that reverts the drift.
            clean = cl.load_catalog(
                out, dry_run=True, on_conflict=cl.ON_CONFLICT_FIXTURE,
                deletions=cl.DELETIONS_IGNORE,
            )
            self.assertEqual(clean.count(cl.ERROR), 0)

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


# ---------------------------------------------------------------------------
# Fable-review regressions (adversarial pass over CAT-1 + CAT-2)
# ---------------------------------------------------------------------------


class ExportPrefilterAfterLoadTests(_CatalogTestCase):
    def test_export_after_load_still_sees_db_only_drift(self):
        """H: the load-written sidecar must not poison export's pre-filter.

        load_catalog used to write the post-load max(revision) into the
        sidecar; the very next export_catalog — the one the db-only-drift
        warning tells the operator to run — then saw an unmoved max(revision)
        and silently skipped, stranding the drift out of canon forever.
        """
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            apparel = Category.objects.get(slug="apparel")
            apparel.name = "Edited in admin"
            apparel.save()

            report = cl.load_catalog(out)          # warns db_only, rewrites sidecar
            self.assertEqual(report.count(cl.DB_ONLY), 1)
            # The load-written sidecar carries no export pre-filter base.
            self.assertNotIn("max_revision", _read_json(out, cf.STATE_FILE))

            _export(out)                            # follow the warning's advice
            self.assertIn("Edited in admin", _read(out, cf.CATEGORIES_FILE))


class FilteredParentLoadabilityTests(_CatalogTestCase):
    def test_export_with_soft_deleted_parent_loads_into_fresh_db(self):
        """H: the default export must always be loadable on a clean DB."""
        self.seed_catalog()                        # electronics -> phones
        Category.objects.get(slug="electronics").soft_delete()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            _wipe_db()
            report = cl.load_catalog(out, seed_if_empty=True)
            self.assertFalse(report.failed)
            phones = Category.objects.get(slug="phones")
            self.assertIsNone(phones.tn_parent_id)  # re-rooted, not dangling

    def test_export_with_is_test_parent_loads_into_fresh_db(self):
        root = Category.objects.create(name="QA root", slug="qa-root", is_test=True)
        Category.objects.create(name="Real child", slug="real-child", tn_parent=root)
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            _wipe_db()
            report = cl.load_catalog(out, seed_if_empty=True)
            self.assertFalse(report.failed)
            self.assertTrue(Category.objects.filter(slug="real-child").exists())


class UnreachableFixtureStateTests(_CatalogTestCase):
    def test_duplicate_bare_ref_is_loud_error_not_perpetual_churn(self):
        """H: two entries resolving to one row can never be materialized —
        the loader used to silently re-"apply" (revision bump + emit) on
        every run forever. Now: per-record error, zero churn."""
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            apparel = next(c for c in cats if c["slug"] == "apparel")
            apparel["features"] = [{"slug": "size"}, {"slug": "size"}]
            _write_json(out, cf.CATEGORIES_FILE, cats)

            rev_before = Category.objects.get(slug="apparel").revision
            first = cl.load_catalog(out)
            self.assertTrue(first.failed)
            self.assertEqual(first.errors, 1)
            second = cl.load_catalog(out)
            self.assertTrue(second.failed)
            self.assertEqual(
                Category.objects.get(slug="apparel").revision, rev_before
            )

    def test_inline_is_test_entry_does_not_churn_revisions(self):
        """H: an is_test inline entry is invisible to the export view, so the
        record can never converge — the loader used to save() the category on
        every run (phantom revisions + emits). The dirty guard stops that."""
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            apparel = next(c for c in cats if c["slug"] == "apparel")
            apparel["features"] = [{
                "slug": "size", "config": {"type": "int", "min": 5},
                "mandatory": False, "show_as_badge": False,
                "show_at_title": False, "translate": "all", "is_test": True,
            }]
            _write_json(out, cf.CATEGORIES_FILE, cats)

            cl.load_catalog(out)
            rev_after_first = Category.objects.get(slug="apparel").revision
            received = _capture_events()
            cl.load_catalog(out)
            self.assertEqual(
                Category.objects.get(slug="apparel").revision, rev_after_first
            )
            self.assertEqual(received, [])

    def test_self_parent_record_is_error(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            next(c for c in cats if c["slug"] == "apparel")["parent_slug"] = "apparel"
            _write_json(out, cf.CATEGORIES_FILE, cats)

            report = cl.load_catalog(out)
            self.assertTrue(report.failed)
            self.assertIsNone(Category.objects.get(slug="apparel").tn_parent_id)


class HardDeleteCascadeGuardTests(_CatalogTestCase):
    def test_hard_delete_refuses_when_live_children_survive(self):
        """H: treenode delete() cascades the subtree — removing only the
        parent from the fixture used to silently take the (still-declared)
        child down with it, while the report listed the child as skipped."""
        self.seed_catalog()                        # electronics -> phones
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = [c for c in _read_json(out, cf.CATEGORIES_FILE)
                    if c["slug"] != "electronics"]
            _write_json(out, cf.CATEGORIES_FILE, cats)

            report = cl.load_catalog(out, deletions=cl.DELETIONS_HARD)
            self.assertTrue(report.failed)
            self.assertEqual(report.errors, 1)
            self.assertTrue(Category.objects.filter(slug="phones").exists())
            self.assertTrue(Category.objects.filter(slug="electronics").exists())

    def test_hard_delete_of_whole_subtree_runs_children_first(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = [c for c in _read_json(out, cf.CATEGORIES_FILE)
                    if c["slug"] not in ("electronics", "phones")]
            _write_json(out, cf.CATEGORIES_FILE, cats)

            report = cl.load_catalog(out, deletions=cl.DELETIONS_HARD)
            self.assertFalse(report.failed)
            self.assertFalse(
                Category.objects.filter(slug__in=["electronics", "phones"]).exists()
            )

    def test_hard_delete_refuses_feature_still_linked_by_live_category(self):
        """Hard-deleting a root feature cascades its override rows and every
        CategoryFeature link — silently stripping categories that still carry
        it. The loader refuses instead."""
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            feats = [f for f in _read_json(out, cf.FEATURES_FILE) if f["slug"] != "color"]
            _write_json(out, cf.FEATURES_FILE, feats)
            # Keep the categories' entries referencing color untouched: an
            # inconsistent fixture must not cause silent schema stripping.
            report = cl.load_catalog(out, deletions=cl.DELETIONS_HARD)
            self.assertTrue(report.failed)
            self.assertTrue(
                Feature.objects.filter(slug="color", tn_parent__isnull=True).exists()
            )
            # The child category's override row survived the (refused) cascade.
            self.assertTrue(Feature.objects.filter(pk=self.override.pk).exists())

    def test_command_errors_when_features_file_missing(self):
        """Missing features.json must not read as 'delete every feature'."""
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            os.remove(os.path.join(out, cf.FEATURES_FILE))
            with self.assertRaises(CommandError):
                _load_cmd(out)
            self.assertFalse(Feature.objects.filter(deleted=True).exists())


# ---------------------------------------------------------------------------
# Source identity: re-imports are keyed by (external_source, external_id)
# ---------------------------------------------------------------------------


class SourceIdentityTests(_CatalogTestCase):
    """A re-import of an external catalogue matches on source identity first.

    The slug of an imported category is derived from the source's node path, so
    the source renaming a node moves the slug while the node stays the same
    node. Matching by slug would read that as "one category vanished, another
    appeared" and leave a duplicate beside the row that holds the listings.
    """

    def _seed_imported(self):
        """Two imported roots + one imported child, all carrying source ids."""
        self.phones = Category.objects.create(
            name="Phones", slug="phones", external_id="129639",
            external_source="catalog-a",
        )
        self.used = Category.objects.create(
            name="Used phones", slug="used-phones", tn_parent=self.phones,
            external_id="129640", external_source="catalog-a",
        )

    def _cats(self, out):
        return {r["slug"]: r for r in _read_json(out, cf.CATEGORIES_FILE)}

    # -- the rename ---------------------------------------------------------

    def test_a_renamed_node_updates_in_place_and_creates_no_duplicate(self):
        self._seed_imported()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            records = _read_json(out, cf.CATEGORIES_FILE)
            for rec in records:
                if rec["external_id"] == "129639":
                    rec["slug"] = "mobile-phones"      # source renamed the node
                    rec["name"] = "Mobile phones"
                elif rec["external_id"] == "129640":
                    rec["parent_slug"] = "mobile-phones"
            _write_json(out, cf.CATEGORIES_FILE, records)

            report = cl.load_catalog(out, on_conflict=cl.ON_CONFLICT_FIXTURE)
            self.assertFalse(report.failed, [(i.kind, i.key, i.detail) for i in report.categories])

            # Same row (same pk), new slug and name — not a second row.
            self.assertEqual(Category.objects.filter(external_id="129639").count(), 1)
            self.assertFalse(Category.objects.filter(slug="phones").exists())
            moved = Category.objects.get(pk=self.phones.pk)
            self.assertEqual(moved.slug, "mobile-phones")
            self.assertEqual(moved.name, "Mobile phones")
            # The child kept hanging off the very same parent row.
            self.assertEqual(
                Category.objects.get(pk=self.used.pk).tn_parent_id, self.phones.pk
            )
            # Reported as an update-with-rename, never as created + deleted.
            kinds = {it.key: it.kind for it in report.categories}
            self.assertEqual(kinds["mobile-phones"], cl.UPDATED)
            self.assertNotIn("phones", kinds)

    def test_the_dry_run_plan_prints_the_rename_and_writes_nothing(self):
        self._seed_imported()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            records = _read_json(out, cf.CATEGORIES_FILE)
            for rec in records:
                if rec["external_id"] == "129639":
                    rec["slug"] = "mobile-phones"
                elif rec["external_id"] == "129640":
                    rec["parent_slug"] = "mobile-phones"
            _write_json(out, cf.CATEGORIES_FILE, records)

            report = cl.load_catalog(out, dry_run=True, on_conflict=cl.ON_CONFLICT_FIXTURE)
            item = next(it for it in report.categories if it.key == "mobile-phones")
            self.assertTrue(item.renamed)
            self.assertEqual(item.kind, cl.UPDATED)
            self.assertIn("slug 'phones' → 'mobile-phones'", item.detail)
            self.assertIn("external_id '129639'", item.detail)
            self.assertEqual(report.renames, 1)
            # No add/remove pair anywhere in the plan for this node.
            self.assertEqual([it.kind for it in report.categories if it.kind == cl.DELETED], [])
            self.assertEqual([it.kind for it in report.categories if it.kind == cl.CREATED], [])
            # Dry run wrote nothing.
            self.assertTrue(Category.objects.filter(slug="phones").exists())

    def test_the_command_prints_the_rename_line(self):
        from io import StringIO

        self._seed_imported()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            records = _read_json(out, cf.CATEGORIES_FILE)
            for rec in records:
                if rec["external_id"] == "129639":
                    rec["slug"] = "mobile-phones"
                elif rec["external_id"] == "129640":
                    rec["parent_slug"] = "mobile-phones"
            _write_json(out, cf.CATEGORIES_FILE, records)

            buf = StringIO()
            call_command(
                "load_catalog", dir=out, dry_run=True,
                on_conflict=cl.ON_CONFLICT_FIXTURE, stdout=buf,
            )
            text = buf.getvalue()
            self.assertIn("of which renamed 1", text)
            self.assertIn(
                "» mobile-phones  (renamed: slug 'phones' → "
                "'mobile-phones' (external_id '129639', source 'catalog-a'))",
                text,
            )

    def test_a_rename_chain_resolves_in_one_run(self):
        """a→b while b→c: the holder of the target slug moves away first."""
        first = Category.objects.create(
            name="A", slug="a", external_id="1", external_source="catalog-a"
        )
        second = Category.objects.create(
            name="B", slug="b", external_id="2", external_source="catalog-a"
        )
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            records = _read_json(out, cf.CATEGORIES_FILE)
            for rec in records:
                rec["slug"] = {"1": "b", "2": "c"}[rec["external_id"]]
            _write_json(out, cf.CATEGORIES_FILE, records)

            report = cl.load_catalog(out, on_conflict=cl.ON_CONFLICT_FIXTURE)
            self.assertFalse(report.failed, [(i.kind, i.key, i.detail) for i in report.categories])
            self.assertEqual(Category.objects.get(pk=first.pk).slug, "b")
            self.assertEqual(Category.objects.get(pk=second.pk).slug, "c")
            self.assertEqual(Category.objects.count(), 2)

    # -- the slug fallback --------------------------------------------------

    def test_a_hand_seeded_row_without_external_id_still_matches_by_slug(self):
        hand = Category.objects.create(name="Services", slug="services")
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            records = _read_json(out, cf.CATEGORIES_FILE)
            records[0]["name"] = "Services & repair"
            _write_json(out, cf.CATEGORIES_FILE, records)

            report = cl.load_catalog(out, on_conflict=cl.ON_CONFLICT_FIXTURE)
            self.assertFalse(report.failed)
            self.assertEqual(Category.objects.count(), 1)
            self.assertEqual(Category.objects.get(pk=hand.pk).name, "Services & repair")

    def test_a_hand_seeded_row_is_adopted_by_the_first_import_that_claims_it(self):
        """A slug match with no live identity may take on the source id."""
        hand = Category.objects.create(name="Phones", slug="phones")
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            records = _read_json(out, cf.CATEGORIES_FILE)
            records[0]["external_id"] = "129639"
            records[0]["external_source"] = "catalog-a"
            _write_json(out, cf.CATEGORIES_FILE, records)

            report = cl.load_catalog(out, on_conflict=cl.ON_CONFLICT_FIXTURE)
            self.assertFalse(report.failed)
            self.assertEqual(Category.objects.count(), 1)
            adopted = Category.objects.get(pk=hand.pk)
            self.assertEqual(adopted.external_id, "129639")
            self.assertEqual(adopted.external_source, "catalog-a")

    def test_external_source_separates_two_catalogues_reusing_one_id(self):
        """Same external_id, different source — two rows, never one."""
        first_source = Category.objects.create(
            name="Catalogue A 1", slug="catalog-a-one", external_id="1", external_source="catalog-a"
        )
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            records = _read_json(out, cf.CATEGORIES_FILE)
            records.append({
                "slug": "catalog-b-one", "parent_slug": None, "name": "Catalogue B 1",
                "comment": "", "catalog_icon": "", "carousel_icon": "",
                "carousel_enabled": False, "active": True, "external_id": "1",
                "external_source": "catalog-b", "translatable": True, "features": [],
            })
            _write_json(out, cf.CATEGORIES_FILE, records)

            report = cl.load_catalog(out, on_conflict=cl.ON_CONFLICT_FIXTURE)
            self.assertFalse(report.failed, [(i.kind, i.key, i.detail) for i in report.categories])
            self.assertEqual(Category.objects.count(), 2)
            self.assertEqual(Category.objects.get(pk=first_source.pk).slug, "catalog-a-one")
            self.assertEqual(
                Category.objects.get(external_source="catalog-b").slug, "catalog-b-one"
            )

    # -- collisions: identity wins, loudly ----------------------------------

    def test_identity_wins_over_a_slug_match_on_a_different_row(self):
        """Fixture identity matches row A, fixture slug is held by row B."""
        row_a = Category.objects.create(
            name="A", slug="a", external_id="1", external_source="catalog-a"
        )
        row_b = Category.objects.create(name="B", slug="b")   # hand-seeded, stays
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            records = _read_json(out, cf.CATEGORIES_FILE)
            for rec in records:
                if rec["external_id"] == "1":
                    rec["slug"] = "b"          # A renamed onto B's slug
                    rec["name"] = "A renamed"
            # B stays in the fixture under its own slug, unchanged — nothing
            # frees the slug, so the rename cannot land.
            records = [r for r in records if r["external_id"] == "1"]
            _write_json(out, cf.CATEGORIES_FILE, records)

            report = cl.load_catalog(out, on_conflict=cl.ON_CONFLICT_FIXTURE,
                                     deletions=cl.DELETIONS_IGNORE)
            self.assertTrue(report.failed)
            errors = [it for it in report.categories if it.kind == cl.ERROR]
            self.assertEqual(len(errors), 1)
            self.assertIn("external_id '1'", errors[0].detail)
            self.assertIn("'a'", errors[0].detail)
            self.assertIn("'b'", errors[0].detail)
            self.assertIn("identity wins", errors[0].detail)

            # Neither row was clobbered and no third row appeared.
            self.assertEqual(Category.objects.count(), 2)
            self.assertEqual(Category.objects.get(pk=row_a.pk).slug, "a")
            b = Category.objects.get(pk=row_b.pk)
            self.assertEqual(b.name, "B")           # NOT overwritten by A's record
            self.assertEqual(b.external_id, "")     # NOT stamped with A's id

    def test_the_dry_run_reports_the_collision_before_anything_is_written(self):
        Category.objects.create(
            name="A", slug="a", external_id="1", external_source="catalog-a"
        )
        Category.objects.create(name="B", slug="b")
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            records = [r for r in _read_json(out, cf.CATEGORIES_FILE)
                       if r["external_id"] == "1"]
            records[0]["slug"] = "b"
            _write_json(out, cf.CATEGORIES_FILE, records)

            report = cl.load_catalog(out, dry_run=True,
                                     on_conflict=cl.ON_CONFLICT_FIXTURE,
                                     deletions=cl.DELETIONS_IGNORE)
            self.assertTrue(report.failed)
            self.assertIn("identity wins",
                          " ".join(it.detail for it in report.categories))

    def test_a_re_stamped_identity_is_applied_but_called_out_in_the_plan(self):
        """Fixture id X, slug held by a row already carrying id Y.

        The fixture stays canon for its own slug (an operator correcting a
        wrong id must work), but swapping which source node a row represents
        is not something a plan may leave unsaid.
        """
        held = Category.objects.create(
            name="Held", slug="phones", external_id="222", external_source="catalog-a"
        )
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            records = _read_json(out, cf.CATEGORIES_FILE)
            records[0]["external_id"] = "111"       # a different source node
            _write_json(out, cf.CATEGORIES_FILE, records)

            plan = cl.load_catalog(out, dry_run=True, on_conflict=cl.ON_CONFLICT_FIXTURE)
            item = next(it for it in plan.categories if it.key == "phones")
            self.assertIn("re-stamped", item.detail)
            self.assertIn("external_id '222'", item.detail)
            self.assertIn("external_id '111'", item.detail)
            self.assertFalse(item.renamed)          # not a rename: the slug held

            report = cl.load_catalog(out, on_conflict=cl.ON_CONFLICT_FIXTURE)
            self.assertFalse(report.failed)
            self.assertEqual(Category.objects.get(pk=held.pk).external_id, "111")
            self.assertEqual(Category.objects.count(), 1)

    def test_duplicate_live_rows_for_one_source_node_are_refused(self):
        Category.objects.create(
            name="One", slug="one", external_id="1", external_source="catalog-a"
        )
        Category.objects.create(
            name="Two", slug="two", external_id="1", external_source="catalog-a"
        )
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            records = _read_json(out, cf.CATEGORIES_FILE)
            for rec in records:
                rec["slug"] = "three"
                rec["parent_slug"] = None
            _write_json(out, cf.CATEGORIES_FILE, [records[0]])

            report = cl.load_catalog(out, on_conflict=cl.ON_CONFLICT_FIXTURE,
                                     deletions=cl.DELETIONS_IGNORE)
            self.assertTrue(report.failed)
            self.assertIn("merge or clear",
                          " ".join(it.detail for it in report.categories))

    # -- the export half ----------------------------------------------------

    def test_external_source_round_trips_and_is_omitted_when_blank(self):
        self._seed_imported()
        Category.objects.create(name="Hand", slug="hand")
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = self._cats(out)
            self.assertEqual(cats["phones"]["external_source"], "catalog-a")
            # Blank source stays out of the record, so hashes written before
            # the field existed keep their meaning.
            self.assertNotIn("external_source", cats["hand"])

            _wipe_db()
            _load_cmd(out, seed_if_empty=True)
            self.assertEqual(
                Category.objects.get(slug="phones").external_source, "catalog-a"
            )
            self.assertEqual(Category.objects.get(slug="hand").external_source, "")

    def test_a_rename_is_idempotent_on_a_second_load(self):
        self._seed_imported()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            records = _read_json(out, cf.CATEGORIES_FILE)
            for rec in records:
                if rec["external_id"] == "129639":
                    rec["slug"] = "mobile-phones"
                elif rec["external_id"] == "129640":
                    rec["parent_slug"] = "mobile-phones"
            _write_json(out, cf.CATEGORIES_FILE, records)

            cl.load_catalog(out, on_conflict=cl.ON_CONFLICT_FIXTURE)
            before = Category.objects.get(pk=self.phones.pk).revision
            report = cl.load_catalog(out, on_conflict=cl.ON_CONFLICT_FIXTURE)
            self.assertFalse(report.failed)
            self.assertEqual(report.renames, 0)   # already applied — not a rename
            self.assertEqual(Category.objects.get(pk=self.phones.pk).revision, before)


# ---------------------------------------------------------------------------
# The tree rebuild is deferred to the end of the load (one rebuild, not N)
# ---------------------------------------------------------------------------


class DeferredTreeRebuildTests(_CatalogTestCase):
    """django-treenode rebuilds the whole table from a post_save receiver.

    Left armed, a load of N rows costs O(N^2) statements — see
    ``catalog_load._deferred_tree_rebuild`` for the measured curve. These tests
    pin the two halves of the fix: the receivers are suspended for the write
    phase and the rebuild happens exactly once, AND the denormalized columns
    are correct afterwards, so nothing is traded for the speed.
    """

    def _export_wipe(self):
        out = tempfile.mkdtemp()
        _export(out)
        _wipe_db()
        return out

    def test_the_tree_is_rebuilt_once_per_model_for_the_whole_load(self):
        from unittest import mock

        self.seed_catalog()
        out = self._export_wipe()
        with mock.patch.object(Category, "update_tree",
                               wraps=Category.update_tree) as cat_tree, \
             mock.patch.object(Feature, "update_tree",
                               wraps=Feature.update_tree) as feat_tree:
            report = cl.load_catalog(out, on_conflict=cl.ON_CONFLICT_FIXTURE,
                                     deletions=cl.DELETIONS_IGNORE)
        self.assertFalse(report.failed)
        # Three categories and three feature rows were written (two roots and
        # the child's inline override); one rebuild each, not one per row.
        self.assertEqual(Category.objects.count(), 3)
        self.assertEqual(Feature.objects.count(), 3)
        self.assertEqual(cat_tree.call_count, 1)
        self.assertEqual(feat_tree.call_count, 1)

    def test_the_denormalized_tree_columns_are_correct_after_a_load(self):
        self.seed_catalog()
        # A third level, so ancestry is more than "root or child".
        Category.objects.create(name="Smartphones", slug="smartphones",
                                tn_parent=self.phones)
        out = self._export_wipe()
        report = cl.load_catalog(out, on_conflict=cl.ON_CONFLICT_FIXTURE,
                                 deletions=cl.DELETIONS_IGNORE)
        self.assertFalse(report.failed)

        electronics = Category.objects.get(slug="electronics")
        phones = Category.objects.get(slug="phones")
        smartphones = Category.objects.get(slug="smartphones")
        # tn_level is 1-based; tn_depth is the height of the subtree below.
        self.assertEqual(
            [electronics.tn_level, phones.tn_level, smartphones.tn_level], [1, 2, 3]
        )
        self.assertEqual(electronics.tn_depth, 2)
        self.assertEqual(smartphones.get_ancestors_pks(),
                         [str(electronics.pk), str(phones.pk)])
        self.assertEqual(electronics.tn_descendants_count, 2)
        self.assertEqual(phones.tn_children_count, 1)
        # The feature tree too: an override hangs under its root.
        override = Feature.objects.get(slug="color", tn_parent__isnull=False)
        self.assertEqual(override.tn_level, 2)
        self.assertEqual(
            override.get_ancestors_pks(),
            [str(Feature.objects.get(slug="color", tn_parent__isnull=True).pk)],
        )

    def test_the_receivers_are_live_again_after_a_load(self):
        self.seed_catalog()
        out = self._export_wipe()
        cl.load_catalog(out, on_conflict=cl.ON_CONFLICT_FIXTURE,
                        deletions=cl.DELETIONS_IGNORE)
        # An ordinary save after the load still maintains the tree by itself.
        parent = Category.objects.get(slug="electronics")
        child = Category.objects.create(name="Cameras", slug="cameras",
                                        tn_parent=parent)
        self.assertEqual(Category.objects.get(pk=child.pk).tn_level, 2)
        self.assertEqual(
            Category.objects.get(pk=child.pk).get_ancestors_pks(), [str(parent.pk)]
        )

    def test_the_receivers_are_live_again_after_a_load_that_raises(self):
        self.seed_catalog()
        out = self._export_wipe()
        # A sidecar from the future makes load_catalog raise out of the apply
        # path; the receivers must not stay disconnected process-wide.
        _write_json(out, cf.STATE_FILE,
                    {"version": "not-a-version", "features": {}, "categories": {}})
        with self.assertRaises(ValueError):
            cl.load_catalog(out, on_conflict=cl.ON_CONFLICT_FIXTURE)

        parent = Category.objects.create(name="Books", slug="books")
        child = Category.objects.create(name="Comics", slug="comics", tn_parent=parent)
        self.assertEqual(Category.objects.get(pk=child.pk).tn_level, 2)


# ---------------------------------------------------------------------------
# Duplicate-shaped rows: hand rows inside canon subtrees, sibling name clashes
# ---------------------------------------------------------------------------


class DbNewInCanonSubtreeTests(_CatalogTestCase):
    """A live category the fixture does not know, PARKED inside a subtree the
    fixture owns, is duplicate-shaped: on a live stand, hand-seeded children
    («Smartphones», «Laptops»…) sat beside imported canon siblings («Phones»,
    «Notebooks»…) under an imported root, and sellers saw near-duplicate
    options — while the report filed the rows under the same generic db_new
    note as any deliberately local root. The parent being fixture-owned is
    what makes it a warning of its own."""

    def _seed_and_export(self, out):
        self.seed_catalog()
        _export(out)

    def test_db_new_under_fixture_owned_parent_is_its_own_warning_kind(self):
        with tempfile.TemporaryDirectory() as out:
            self._seed_and_export(out)
            Category.objects.create(
                name="Smartphones", slug="smartphones-hand", tn_parent=self.electronics
            )
            report = cl.load_catalog(out)
            self.assertFalse(report.failed)
            items = {it.key: it for it in report.categories}
            item = items.get("smartphones-hand")
            self.assertIsNotNone(item)
            self.assertEqual(item.kind, cl.DB_NEW_IN_CANON)
            self.assertIn("electronics", item.detail)   # names the canon parent
            self.assertIn("smartphones-hand", item.key)

    def test_db_new_at_a_root_the_fixture_does_not_own_stays_generic(self):
        with tempfile.TemporaryDirectory() as out:
            self._seed_and_export(out)
            Category.objects.create(name="Local services", slug="local-services")
            report = cl.load_catalog(out)
            items = {it.key: it for it in report.categories}
            self.assertEqual(items["local-services"].kind, cl.DB_NEW)

    def test_dry_run_plan_carries_the_same_warning(self):
        with tempfile.TemporaryDirectory() as out:
            self._seed_and_export(out)
            Category.objects.create(
                name="Smartphones", slug="smartphones-hand", tn_parent=self.electronics
            )
            report = cl.load_catalog(out, dry_run=True)
            items = {it.key: it for it in report.categories}
            self.assertEqual(items["smartphones-hand"].kind, cl.DB_NEW_IN_CANON)

    def test_command_prints_a_line_for_it(self):
        from io import StringIO

        from django.core.management import call_command

        with tempfile.TemporaryDirectory() as out:
            self._seed_and_export(out)
            Category.objects.create(
                name="Smartphones", slug="smartphones-hand", tn_parent=self.electronics
            )
            stdout = StringIO()
            call_command("load_catalog", dir=out, stdout=stdout)
            text = stdout.getvalue()
            self.assertIn("smartphones-hand", text)
            self.assertIn("electronics", text)


class SiblingNameCollisionTests(_CatalogTestCase):
    """Two live, active siblings carrying the same case-folded name is the
    state a seller actually experiences as a duplicate — the stand's real
    case was two active «Другое» under «Растения». Nothing enforced it and
    nothing even said it."""

    def test_same_folded_name_under_one_parent_warns_after_load(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            Category.objects.create(
                name="Другое", slug="other-1", tn_parent=self.apparel
            )
            Category.objects.create(
                name="другое", slug="other-2", tn_parent=self.apparel
            )
            report = cl.load_catalog(out)
            self.assertFalse(report.failed)   # a warning, not a gate
            collisions = [
                it for it in report.categories if it.kind == cl.NAME_COLLISION
            ]
            self.assertEqual(len(collisions), 1)
            self.assertIn("other-1", collisions[0].detail)
            self.assertIn("other-2", collisions[0].detail)
            self.assertIn("apparel", collisions[0].key)

    def test_inactive_or_deleted_siblings_do_not_trigger(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            Category.objects.create(
                name="Другое", slug="other-1", tn_parent=self.apparel
            )
            Category.objects.create(
                name="Другое", slug="other-2", tn_parent=self.apparel, active=False
            )
            Category.objects.create(
                name="Другое", slug="other-3", tn_parent=self.apparel, deleted=True
            )
            report = cl.load_catalog(out)
            self.assertEqual(
                [it for it in report.categories if it.kind == cl.NAME_COLLISION], []
            )

    def test_dry_run_plan_output_warns_too(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            Category.objects.create(
                name="Другое", slug="other-1", tn_parent=self.apparel
            )
            Category.objects.create(
                name="Другое", slug="other-2", tn_parent=self.apparel
            )
            report = cl.load_catalog(out, dry_run=True)
            self.assertTrue(
                any(it.kind == cl.NAME_COLLISION for it in report.categories)
            )

    def test_command_prints_a_human_line(self):
        from io import StringIO

        from django.core.management import call_command

        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            Category.objects.create(
                name="Другое", slug="other-1", tn_parent=self.apparel
            )
            Category.objects.create(
                name="Другое", slug="other-2", tn_parent=self.apparel
            )
            stdout = StringIO()
            call_command("load_catalog", dir=out, stdout=stdout)
            text = stdout.getvalue()
            self.assertIn("other-1", text)
            self.assertIn("other-2", text)


# ---------------------------------------------------------------------------
# Convergence: a second load over an unchanged fixture is a no-op, even where
# the applied row cannot hash equal to the record that asked for it (0.20.2)
# ---------------------------------------------------------------------------


class ConvergenceTests(_CatalogTestCase):
    """The 431 → 303 → 303 → … loop, and the three shapes that caused it.

    Measured on a live catalogue (3444 categories, 3192 root features,
    ``--on-conflict fixture-wins --deletions ignore``): after a full load, 303
    categories re-planned ``updated`` on every subsequent pass and never
    converged — zero conflicts, zero creates, and an apply that wrote nothing
    (the dirty guard in ``_apply_category_upsert`` held). The base advanced to
    the APPLIED DB HASH, so a record whose DB state can never equal its
    fixture hash was compared, forever, against a number it could not reach.
    """

    def _load(self, out):
        return cl.load_catalog(out, on_conflict=cl.ON_CONFLICT_FIXTURE,
                               deletions=cl.DELETIONS_IGNORE)

    def _catalogue(self):
        """A root feature, a category referencing it, and a category whose
        entry is an OVERRIDE clone of it (the shared-namespace shape)."""
        make = Feature.objects.create(
            name="Root Make", slug="make",
            config={"type": "select", "options": _opts("bmw", "audi")},
        )
        transport = Category.objects.create(name="Transport", slug="transport")
        cars = Category.objects.create(name="Cars", slug="cars", tn_parent=transport)
        CategoryFeature.objects.create(category=cars, feature=make, order=0)
        clone = Feature.objects.create(
            tn_parent=make, slug="make", name="Root Make",
            config={"type": "select", "options": _opts("bmw", "audi", "kia")},
        )
        moto = Category.objects.create(name="Moto", slug="moto", tn_parent=transport)
        CategoryFeature.objects.create(category=moto, feature=clone, order=0)
        return make, cars, moto

    def test_an_entry_naming_itself_exactly_as_its_root_converges(self):
        """Class 1: the fixture states a name for an override that EQUALS the
        root's, so export writes no ``name`` there at all — the record hashes
        differently from the row it just wrote, on every pass."""
        self._catalogue()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            rec = next(c for c in cats if c["slug"] == "cars")
            rec["features"] = [{
                "slug": "make", "name": "Root Make",
                "config": {"type": "select", "options": _opts("bmw", "audi")},
            }]
            _write_json(out, cf.CATEGORIES_FILE, cats)

            first = self._load(out)
            self.assertEqual(first.count(cl.UPDATED), 1)
            second = self._load(out)
            self.assertEqual(second.count(cl.UPDATED), 0)
            self.assertEqual(second.count(cl.CREATED), 0)
            self.assertEqual(second.count(cl.RESIDUAL), 0)
            self.assertEqual(self._load(out).count(cl.UPDATED), 0)

    def test_an_unnamed_override_follows_its_root_and_converges(self):
        """Class 2: canon renames the root; the category's entry says nothing
        about the override's name — which means "the root's", the rule export
        writes by. The clone kept the OLD label (a seller saw the retired one)
        and export wrote it out, so the record never converged."""
        make, _cars, _moto = self._catalogue()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            feats = _read_json(out, cf.FEATURES_FILE)
            next(f for f in feats if f["slug"] == "make")["name"] = "Make"
            _write_json(out, cf.FEATURES_FILE, feats)

            first = self._load(out)
            self.assertEqual(first.count(cl.RESIDUAL), 0)
            # The category plan is classified against the DB as it stands
            # BEFORE the feature upserts of the same run, so a root rename
            # reaches the categories that inherit its label on the NEXT pass
            # (there, as db-side drift the fixture policy reverts). That is one
            # extra pass, not an endless one — which is the whole point.
            second = self._load(out)
            self.assertEqual(second.count(cl.UPDATED), 1)
            moto = Category.objects.get(slug="moto")
            override = moto.category_features.get().feature
            self.assertIsNotNone(override.tn_parent_id)
            self.assertEqual(override.name, "Make")  # pulled back to the root
            self.assertEqual(Feature.objects.get(pk=make.pk).name, "Make")

            third = self._load(out)
            self.assertEqual(third.count(cl.UPDATED), 0)
            self.assertEqual(self._load(out).count(cl.UPDATED), 0)

    def test_a_state_the_fixture_cannot_spell_converges_and_is_said_once(self):
        """Class 3: a record whose applied row is legitimately unhashable as
        the fixture — an ``is_test`` inline entry the export view excludes.
        The write goes in, the record converges, and the difference is
        REPORTED (residual) instead of re-planned as ``updated`` forever."""
        self._catalogue()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            rec = next(c for c in cats if c["slug"] == "cars")
            rec["features"] = rec["features"] + [{
                "slug": "", "name": "Scratch", "is_test": True,
                "config": {"type": "string"},
            }]
            _write_json(out, cf.CATEGORIES_FILE, cats)

            first = self._load(out)
            residuals = [it for it in first.categories if it.kind == cl.RESIDUAL]
            self.assertEqual([it.key for it in residuals], ["cars"])
            self.assertIn("features", residuals[0].detail)
            self.assertFalse(first.failed)  # not a conflict, not an error

            second = self._load(out)
            self.assertEqual(second.count(cl.UPDATED), 0)
            self.assertEqual(second.count(cl.RESIDUAL), 0)  # said once, not per pass
            self.assertEqual(self._load(out).count(cl.UPDATED), 0)

    def test_a_db_side_edit_after_convergence_still_conflicts(self):
        """Convergence must not blunt the 3-way diff: once a record is synced
        at its pair, a real DB-side edit moves the DB half and is seen."""
        self._catalogue()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            rec = next(c for c in cats if c["slug"] == "cars")
            rec["features"] = rec["features"] + [{
                "slug": "", "name": "Scratch", "is_test": True,
                "config": {"type": "string"},
            }]
            _write_json(out, cf.CATEGORIES_FILE, cats)
            self._load(out)
            self.assertEqual(self._load(out).count(cl.UPDATED), 0)

            cars = Category.objects.get(slug="cars")
            cars.comment = "edited in the admin"
            cars.save()

            drift = cl.load_catalog(out)  # default policy: abort
            self.assertEqual([it.kind for it in drift.categories if it.key == "cars"],
                             [cl.DB_ONLY])

            cats = _read_json(out, cf.CATEGORIES_FILE)
            next(c for c in cats if c["slug"] == "cars")["name"] = "Fixture Cars"
            _write_json(out, cf.CATEGORIES_FILE, cats)
            both = cl.load_catalog(out)
            self.assertEqual([it.kind for it in both.categories if it.key == "cars"],
                             [cl.CONFLICT])
            self.assertTrue(both.failed)

    def test_a_version_4_sidecar_is_read_and_upgraded_in_place(self):
        """The bare-string entries an older loader (and every export) writes
        are read as both halves of the pair, so an upgrade needs no
        regeneration — and the first load that touches a key upgrades it."""
        self._catalogue()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            state = _read_json(out, cf.STATE_FILE)
            state["version"] = 4
            self.assertTrue(all(isinstance(v, str) for v in state["categories"].values()))
            _write_json(out, cf.STATE_FILE, state)

            unchanged = self._load(out)
            self.assertEqual(unchanged.count(cl.UPDATED), 0)  # classified as before

            cats = _read_json(out, cf.CATEGORIES_FILE)
            next(c for c in cats if c["slug"] == "cars")["comment"] = "canon says so"
            _write_json(out, cf.CATEGORIES_FILE, cats)
            self.assertEqual(self._load(out).count(cl.UPDATED), 1)

            state_after = _read_json(out, cf.STATE_FILE)
            self.assertEqual(state_after["version"], 5)
            self.assertEqual(set(state_after["categories"]["cars"]), {"fixture", "db"})
            # untouched keys keep the form they had
            self.assertIsInstance(state_after["categories"]["moto"], str)
            self.assertEqual(self._load(out).count(cl.UPDATED), 0)


class UnsaidKeyTests(_CatalogTestCase):
    """A fixture must not erase what it does not state (0.20.3).

    The live failure: a full ``--on-conflict fixture-wins`` reload blanked
    every ``children_axis_label`` ``derive_children_as --apply`` had written —
    three chip rows came back answering ``''`` and the fourth kept its caption
    only because that one record happened to spell it out. 0.20.0 put the
    column in ``_CATEGORY_SCALARS``; the export writes it only when set, so an
    absent key reached ``_apply_category_upsert`` as ``""``.

    The rule pinned here: an ABSENT optional key keeps the live value, an
    EXPLICIT one is applied, ``""`` included — and an unsaid key produces no
    ``updated`` row, because nothing on the fixture side moved.
    """

    def _load(self, out, **kwargs):
        kwargs.setdefault("on_conflict", cl.ON_CONFLICT_FIXTURE)
        return cl.load_catalog(out, **kwargs)

    def test_an_absent_axis_label_survives_a_fixture_wins_reload(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            # Written after the export, exactly as `derive_children_as
            # --apply` writes it: a targeted UPDATE the fixture never learns of.
            Category.objects.filter(slug="electronics").update(
                children_as_derived="chips",
                children_axis_label="categories.axis.condition",
            )
            self.assertNotIn(
                "children_axis_label",
                next(c for c in _read_json(out, cf.CATEGORIES_FILE)
                     if c["slug"] == "electronics"),
            )

            report = self._load(out)

            self.assertFalse(report.failed)
            self.assertEqual(
                Category.objects.get(slug="electronics").children_axis_label,
                "categories.axis.condition",
            )

    def test_an_unsaid_key_is_not_an_update(self):
        """The plan half of the rule: nothing moved on the fixture side, so
        the operator does not read `updated` for a row the load leaves alone
        (and a policy that reverts db drift has nothing to revert)."""
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            Category.objects.filter(slug="electronics").update(
                children_axis_label="categories.axis.condition"
            )

            plan = cl.load_catalog(out, dry_run=True)

            kinds = {it.key: it.kind for it in plan.categories}
            self.assertEqual(kinds["electronics"], cl.SKIPPED)
            self.assertEqual(plan.count(cl.UPDATED), 0)
            self.assertEqual(plan.count(cl.DB_ONLY), 0)

    def test_an_explicit_empty_string_clears_the_column_and_converges(self):
        """The escape hatch: absence says nothing, `""` says "clear it". And
        it says it ONCE — the next pass is a no-op, not a residual forever."""
        self.seed_catalog()
        Category.objects.filter(slug="electronics").update(
            children_axis_label="categories.axis.condition"
        )
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            next(c for c in cats if c["slug"] == "electronics")["children_axis_label"] = ""
            _write_json(out, cf.CATEGORIES_FILE, cats)

            first = self._load(out)
            self.assertEqual(first.count(cl.UPDATED), 1)
            self.assertEqual(
                Category.objects.get(slug="electronics").children_axis_label, ""
            )

            second = self._load(out)
            self.assertEqual(second.count(cl.UPDATED), 0)
            self.assertEqual(second.count(cl.RESIDUAL), 0)
            self.assertEqual(self._load(out).count(cl.UPDATED), 0)

    def test_every_optional_scalar_is_kept_not_blanked(self):
        """The audit, not just the one column that was reported: every scalar
        the loader owns and a record may leave out."""
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            live = {
                "children_as": "chips",
                "children_axis_label": "categories.axis.condition",
                "comment": "for translators",
                "external_id": "42",
                "external_source": "partner-feed",
                "translatable": False,
            }
            Category.objects.filter(slug="electronics").update(**live)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            record = next(c for c in cats if c["slug"] == "electronics")
            for key in live:
                record.pop(key, None)
            _write_json(out, cf.CATEGORIES_FILE, cats)

            report = self._load(out)

            self.assertFalse(report.failed)
            row = Category.objects.get(slug="electronics")
            for key, value in live.items():
                self.assertEqual(getattr(row, key), value, key)
            self.assertEqual(report.kept_unsaid, {key: 1 for key in live})

    def test_curation_keys_are_still_write_once(self):
        """The keys that got this cure two releases earlier keep it: an
        UPDATE never writes them, whether the record states them or not."""
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            Category.objects.filter(slug="electronics").update(
                carousel_enabled=True, catalog_icon="catalog/x", active=False
            )
            cats = _read_json(out, cf.CATEGORIES_FILE)
            record = next(c for c in cats if c["slug"] == "electronics")
            record["carousel_enabled"] = False
            record["catalog_icon"] = ""
            record["active"] = True
            _write_json(out, cf.CATEGORIES_FILE, cats)

            self._load(out)

            row = Category.objects.get(slug="electronics")
            self.assertTrue(row.carousel_enabled)
            self.assertEqual(row.catalog_icon, "catalog/x")
            self.assertFalse(row.active)

    def test_a_created_row_takes_the_field_defaults(self):
        """There is nothing to keep on a row that does not exist yet."""
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            cats.append({"slug": "toys", "parent_slug": None, "name": "Toys"})
            _write_json(out, cf.CATEGORIES_FILE, cats)

            self._load(out)

            toys = Category.objects.get(slug="toys")
            self.assertEqual(toys.children_as, "auto")
            self.assertEqual(toys.children_axis_label, "")
            self.assertEqual(toys.comment, "")
            self.assertEqual(toys.external_id, "")
            self.assertTrue(toys.translatable)

    def test_the_report_counts_only_values_worth_keeping(self):
        """A default kept is nothing kept — a count including those would read
        as the size of the catalogue and mean nothing."""
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            self.assertEqual(cl.load_catalog(out, dry_run=True).kept_unsaid, {})

            Category.objects.filter(slug="electronics").update(
                children_axis_label="categories.axis.condition"
            )
            self.assertEqual(
                cl.load_catalog(out, dry_run=True).kept_unsaid,
                {"children_axis_label": 1},
            )

    def test_the_command_says_how_many_values_it_kept(self):
        self.seed_catalog()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            Category.objects.filter(slug="electronics").update(
                children_axis_label="categories.axis.condition"
            )
            buf = io.StringIO()

            _load_cmd(out, stdout=buf)

            self.assertIn(
                "kept 1 live value(s) the fixture does not state "
                "(children_axis_label 1)",
                buf.getvalue(),
            )

    def test_a_renamed_node_keeps_its_own_values_not_its_predecessors(self):
        """The kept values are re-keyed onto a source rename like every other
        slug-keyed map, or a renamed row would read another row's blanks."""
        self.seed_catalog()
        Category.objects.filter(slug="apparel").update(
            external_source="partner-feed", external_id="7",
            children_axis_label="categories.axis.audience",
        )
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            cats = _read_json(out, cf.CATEGORIES_FILE)
            record = next(c for c in cats if c["slug"] == "apparel")
            record["slug"] = "clothing"
            record.pop("children_axis_label")
            _write_json(out, cf.CATEGORIES_FILE, cats)

            report = self._load(out)

            self.assertFalse(report.failed)
            moved = Category.objects.get(slug="clothing")
            self.assertEqual(moved.children_axis_label, "categories.axis.audience")
            self.assertFalse(Category.objects.filter(slug="apparel").exists())
