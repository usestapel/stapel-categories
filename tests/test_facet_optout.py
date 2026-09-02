"""``config.facet: false`` — the buyer-facet opt-out, end to end (Д74).

A category's feature list is BOTH the seller's form and the buyer's filter
panel. Most fields are honestly both; a few are neither — the parcel's
weight/length/height/width and the wholesale block are commerce metadata the
seller states about the SALE, not axes anybody shops along. On a live stand
the whole facet panel of «Аквариум» was exactly those four delivery numbers.

``stapel-search``'s ``_is_facetable`` already reads a ``facet`` flag off the
resolved feature (or off its ``config``) and defaults to true, so the opt-out
is a one-key statement — but a key nothing can WRITE is not a mechanism. This
module is the load-bearing half: the key has to survive a catalogue import
(``load_catalog``), a round trip through ``export_catalog``, an inline
per-category override, and the ``categories.features`` boundary that
stapel-search actually reads.

``config`` is opaque to the FeatureDef canon (see
``test_resolved_feature_contract``), so this needs no schema change — only
proof that nothing on the path drops it.
"""
import tempfile

from stapel_categories import catalog_fixtures as cf
from stapel_categories import catalog_load as cl
from stapel_categories.models import Category, CategoryFeature, Feature

from .test_catalog_load import (
    _CatalogTestCase,
    _export,
    _load_cmd,
    _read,
    _read_json,
    _wipe_db,
    _write_json,
)

WEIGHT = {"type": "int", "min": 0, "facet": False}


class FacetOptOutFixtureTests(_CatalogTestCase):
    """A fixture that says ``facet: false`` produces a row that says it."""

    def _fixture(self, out, *, override_facet=None):
        """Write a minimal two-level catalogue carrying the opt-out."""
        _write_json(out, cf.FEATURES_FILE, [
            {"slug": "weight_for_delivery", "name": "Вес (Для Доставки)",
             "config": dict(WEIGHT), "group": "Доставка"},
            {"slug": "color", "name": "Color",
             "config": {"type": "select",
                        "options": [{"value": "red", "label": "red"}]}},
        ])
        entry = {"slug": "weight_for_delivery"}
        if override_facet is not None:
            entry = {"slug": "weight_for_delivery",
                     "config": {"type": "int", "min": 1, "facet": override_facet},
                     "group": "Доставка"}
        _write_json(out, cf.CATEGORIES_FILE, [
            {"slug": "electronics", "parent_slug": None, "name": "Electronics",
             "features": [{"slug": "color"}, {"slug": "weight_for_delivery"}]},
            {"slug": "phones", "parent_slug": "electronics", "name": "Phones",
             "features": [{"slug": "color"}, entry]},
        ])

    def test_the_opt_out_survives_the_loader_into_the_stored_config(self):
        with tempfile.TemporaryDirectory() as out:
            self._fixture(out)
            _wipe_db()
            report = cl.load_catalog(out, seed_if_empty=True)
            self.assertFalse(report.failed, report.failed)

            feature = Feature.objects.get(slug="weight_for_delivery",
                                          tn_parent__isnull=True)
            self.assertIs(feature.config.get("facet"), False)

    def test_the_opt_out_crosses_the_categories_features_boundary(self):
        """What stapel-search's ``_is_facetable`` actually reads."""
        with tempfile.TemporaryDirectory() as out:
            self._fixture(out)
            _wipe_db()
            cl.load_catalog(out, seed_if_empty=True)

            defs = Category.objects.get(slug="electronics").feature_defs()
            by_slug = {d["slug"]: d for d in defs}
            self.assertIs(by_slug["weight_for_delivery"]["config"].get("facet"), False)
            # A feature that says nothing keeps today's behaviour: no key at
            # all, so the consumer's default-to-true is what answers.
            self.assertNotIn("facet", by_slug["color"]["config"])

    def test_an_inline_override_may_opt_back_in(self):
        """The flag is per (feature, category), like every other inline key."""
        with tempfile.TemporaryDirectory() as out:
            self._fixture(out, override_facet=True)
            _wipe_db()
            cl.load_catalog(out, seed_if_empty=True)

            root = {d["slug"]: d for d in
                    Category.objects.get(slug="electronics").feature_defs()}
            child = {d["slug"]: d for d in
                     Category.objects.get(slug="phones").feature_defs()}
            self.assertIs(root["weight_for_delivery"]["config"].get("facet"), False)
            self.assertIs(child["weight_for_delivery"]["config"].get("facet"), True)

    def test_a_second_load_of_the_same_opt_out_is_a_no_op(self):
        """The idempotency hole: a key the export shape drops re-writes forever."""
        with tempfile.TemporaryDirectory() as out:
            self._fixture(out, override_facet=True)
            _wipe_db()
            cl.load_catalog(out, seed_if_empty=True)

            second = cl.load_catalog(out)
            self.assertEqual(second.count(cl.UPDATED), 0)
            self.assertEqual(second.count(cl.CREATED), 0)
            self.assertFalse(second.failed)


class FacetOptOutRoundTripTests(_CatalogTestCase):
    def test_export_reproduces_the_flag_byte_for_byte(self):
        """``export_catalog`` is the other half of the 3-way diff."""
        self.seed_catalog()
        weight = Feature.objects.create(
            name="Вес (Для Доставки)", slug="weight_for_delivery",
            config=dict(WEIGHT), group="Доставка",
        )
        CategoryFeature.objects.create(
            category=Category.objects.get(slug="electronics"), feature=weight, order=2)

        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            _export(first)
            exported = _read_json(first, cf.FEATURES_FILE)
            record = next(r for r in exported if r["slug"] == "weight_for_delivery")
            self.assertIs(record["config"].get("facet"), False)

            _wipe_db()
            _load_cmd(first, seed_if_empty=True)
            _export(second)
            self.assertEqual(_read(second, cf.FEATURES_FILE),
                             _read(first, cf.FEATURES_FILE))


class FacetOptOutEditingSurfaceTests(_CatalogTestCase):
    """The paths that REWRITE a config must not quietly re-enable the facet.

    An operator opening «Вес (Для Доставки)» in the admin, or the feature
    editor pushing a category's list back, hands the config through a form and
    a serializer. Either one that rebuilt the dict from a typed shape would
    drop an engine-level key it does not declare — and the failure is silent:
    the form saves, the row looks right, and the delivery weight is back in
    the buyer's filter panel on the next facet plan.
    """

    def test_the_admin_form_keeps_the_key_it_does_not_declare(self):
        from stapel_categories.forms import FeatureAdminForm

        feature = Feature.objects.create(
            name="Вес (Для Доставки)", slug="weight_for_delivery",
            config=dict(WEIGHT), group="Доставка",
        )
        form = FeatureAdminForm(
            instance=feature,
            data={"name": feature.name, "slug": feature.slug,
                  "config": '{"type": "int", "min": 0, "facet": false}',
                  "translate": "all", "visibility": "public", "tn_priority": 0},
        )
        assert form.is_valid(), form.errors
        assert form.cleaned_data["config"].get("facet") is False

    def test_the_feature_editor_keeps_the_key_through_an_edit(self):
        from stapel_categories.feature_editor import (
            FeatureEditorItem, apply_feature_editor_changes,
        )

        category = Category.objects.create(name="Aquarium", slug="aquarium")
        feature = Feature.objects.create(
            name="Вес (Для Доставки)", slug="weight_for_delivery",
            config=dict(WEIGHT), group="Доставка",
        )
        CategoryFeature.objects.create(category=category, feature=feature, order=0)

        revision = Category.objects.values_list("revision", flat=True).get(pk=category.pk)
        apply_feature_editor_changes(category, [FeatureEditorItem(
            action="edit", order=0,
            feature={"id": feature.pk, "slug": "weight_for_delivery",
                     "name": "Вес посылки", "config": dict(WEIGHT)},
        )], base_revision=revision)

        feature.refresh_from_db()
        assert feature.name == "Вес посылки"
        assert feature.config.get("facet") is False

    def test_the_composer_payload_carries_the_key_too(self):
        """``FeatureCompactSerializer`` is the seller form's own read."""
        from stapel_categories.serializers import FeatureCompactSerializer

        category = Category.objects.create(name="Aquarium", slug="aquarium")
        feature = Feature.objects.create(
            name="Вес (Для Доставки)", slug="weight_for_delivery",
            config=dict(WEIGHT), group="Доставка",
        )
        CategoryFeature.objects.create(category=category, feature=feature, order=0)

        payload = FeatureCompactSerializer(category.get_all_features(), many=True).data
        assert payload[0]["config"].get("facet") is False
