"""Round-trip stability (§6.3) on DIRTY catalog states (fable review, CAT-1+2).

The clean-state round-trip (seed -> export -> load -> export byte-identical)
lives in test_catalog_load.RoundTripTests. Real catalogs are messier; each
test here seeds a state the editor/admin can actually produce and asserts the
full contract: export -> load into a clean DB -> re-export is byte-identical,
AND a subsequent load of the re-export is a zero-write no-op (§6.2).

States covered: a category linking both the root and an override of one slug;
multiple (and shared) slug-less header rows; an override-of-override chain;
links to soft-deleted features; duplicate CategoryFeature.order values.
"""
import tempfile

from django.core.management import call_command

from stapel_categories import catalog_fixtures as cf
from stapel_categories import catalog_load as cl
from stapel_categories.models import Category, CategoryFeature, Feature

from .test_catalog_load import _CatalogTestCase, _opts, _read, _wipe_db


def _export(out_dir, **kwargs):
    call_command("export_catalog", out=out_dir, **kwargs)


class DirtyRoundTripTests(_CatalogTestCase):
    def _round_trip_is_stable(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            _export(first)
            feats = _read(first, cf.FEATURES_FILE)
            cats = _read(first, cf.CATEGORIES_FILE)
            _wipe_db()
            report = cl.load_catalog(first, seed_if_empty=True)
            assert not report.failed, [
                (i.kind, i.key, i.detail)
                for i in report.features + report.categories
                if i.kind in (cl.ERROR, cl.CONFLICT)
            ]
            _export(second)
            assert _read(second, cf.FEATURES_FILE) == feats, "features.json drifted"
            assert _read(second, cf.CATEGORIES_FILE) == cats, "categories.json drifted"
            # And the reloaded DB must be idempotent for a subsequent load.
            report2 = cl.load_catalog(second)
            assert not report2.failed
            assert report2.count(cl.UPDATED) == 0 and report2.count(cl.CREATED) == 0

    def test_category_linking_root_and_override_of_same_slug(self):
        self.seed_catalog()
        # Dirty: phones links BOTH the root color and its override.
        CategoryFeature.objects.create(category=self.phones, feature=self.color, order=7)
        self._round_trip_is_stable()

    def test_two_slugless_header_rows_same_and_different(self):
        self.seed_catalog()
        h1 = Feature.objects.create(name="Section A", slug="", config={"type": "header"})
        h2 = Feature.objects.create(name="Section A", slug="", config={"type": "header"})
        h3 = Feature.objects.create(name="Section B", slug="", config={"type": "header"})
        for i, h in enumerate((h1, h2, h3)):
            CategoryFeature.objects.create(category=self.apparel, feature=h, order=10 + i)
        self._round_trip_is_stable()

    def test_shared_slugless_row_across_categories(self):
        self.seed_catalog()
        h = Feature.objects.create(name="Shared header", slug="", config={"type": "header"})
        CategoryFeature.objects.create(category=self.electronics, feature=h, order=9)
        CategoryFeature.objects.create(category=self.apparel, feature=h, order=0)
        self._round_trip_is_stable()

    def test_deep_override_chain(self):
        self.seed_catalog()
        # override of override (inherit applied twice down a chain)
        deeper = Feature.objects.create(
            tn_parent=self.override, name="Color", slug="color",
            config={"type": "select", "options": _opts("only-black")},
        )
        laptops = Category.objects.create(
            name="Laptops", slug="laptops", tn_parent=self.phones,
        )
        link = laptops.category_features.get(feature__slug="color")
        link.feature = deeper
        link.save()
        self._round_trip_is_stable()

    def test_link_to_soft_deleted_feature_is_dropped_cleanly(self):
        self.seed_catalog()
        self.size.soft_delete()  # electronics still links it
        self._round_trip_is_stable()

    def test_duplicate_order_values(self):
        self.seed_catalog()
        # Dirty: two links share order=0 (id tiebreak).
        CategoryFeature.objects.filter(category=self.electronics).update(order=0)
        self._round_trip_is_stable()
