"""``Feature.visibility`` — the disclosure axis, and every seam it must cross.

stapel-attributes 0.8.0 added ``FeatureDef.visibility``: *which audience may
READ a stored value*. It exists for attributes that IDENTIFY a specific
physical unit rather than describe it — a VIN, an IMEI, a serial or a registry
number — because publishing one lets a stranger act as that unit's owner.

This module is where the axis is actually SET: ``Feature`` rows are the
FeatureDef rows of the fleet, so without a column here the axis cannot be
turned on for any real feature. Nothing here enforces the hiding — that is
stapel-listings' half, off the stamp attributes writes into the stored value —
so what can go wrong in THIS repository is a boundary that keeps quiet about
the field. A dropped ``visibility`` does not fail; it answers ``public``, which
is exactly the publication the axis exists to prevent. Hence one pinned
crossing per seam below.

The one behaviour this module *does* own: a non-public feature is never a title
and never a badge. ``FeatureDef.__post_init__`` resolves that contradiction
downstream, but resolving it downstream still leaves the contradictory ROW in
the table for the next reader to resolve again — so ``Feature`` forces the two
display flags off in ``clean()`` AND in ``save()``, and the admin, the feature
editor and the catalog loader all inherit the coercion for free.
"""
import pytest
from django.contrib.admin.sites import AdminSite
from django.core.exceptions import ValidationError
from django.test import RequestFactory
from stapel_attributes.visibility import OWNER, PUBLIC, STAFF, VISIBILITIES

from stapel_categories.admin import FeatureAdmin
from stapel_categories.feature_editor import FeatureEditorItem, apply_feature_editor_changes
from stapel_categories.models import Category, CategoryFeature, Feature
from stapel_categories.serializers import (
    FeatureCompactSerializer,
    FeatureEditorFeatureSerializer,
    FeatureSerializer,
)

pytestmark = pytest.mark.django_db

BASE = "/catalog/api"


def _feature(**kwargs) -> Feature:
    payload = {
        "name": "VIN",
        "slug": "vin",
        "config": {"type": "string", "maxLength": 17},
        "mandatory": True,
    }
    payload.update(kwargs)
    return Feature.objects.create(**payload)


@pytest.fixture
def vin() -> Feature:
    """The canonical non-public feature: mandatory, validated, and hidden."""
    return _feature(visibility=OWNER)


@pytest.fixture
def category(vin) -> Category:
    category = Category.objects.create(name="Cars", slug="cars")
    CategoryFeature.objects.create(category=category, feature=vin, order=0)
    return category


# ── the column ───────────────────────────────────────────────────────


class TestTheDefaultIsPublic:
    def test_a_new_feature_is_public(self):
        assert _feature().visibility == PUBLIC

    def test_the_choices_mirror_the_engine(self):
        """The model spells the vocabulary out; the engine owns it.

        A ``TextChoices`` is written by hand (like ``TranslateMode``) so the
        migration state is stable, which means the two can drift. They must
        not: an upstream value this column cannot store is a disclosure level
        the catalogue cannot express.
        """
        assert tuple(Feature.Visibility.values) == VISIBILITIES

    def test_a_row_written_before_the_axis_reads_as_public(self):
        """The migration's default, seen the way an existing row sees it.

        ``update()`` writes the column without going through ``save()``, which
        is the closest a test gets to a row that predates the field.
        """
        feature = _feature()
        Feature.objects.filter(pk=feature.pk).update(visibility="")
        feature.refresh_from_db()
        feature.clean()
        assert feature.visibility == PUBLIC

    def test_an_unknown_visibility_is_refused_not_downgraded(self):
        """A typo must not quietly publish a VIN."""
        feature = _feature()
        feature.visibility = "private"
        with pytest.raises(ValidationError) as exc:
            feature.clean()
        assert "visibility" in exc.value.message_dict


class TestNonPublicIsNeverATitleOrABadge:
    """The contradiction is resolved in the only direction that cannot leak."""

    def test_clean_silences_the_display_flags(self):
        feature = _feature(visibility=STAFF, show_at_title=True, show_as_badge=True)
        feature.clean()
        assert feature.show_at_title is False
        assert feature.show_as_badge is False

    def test_save_silences_them_too_so_the_row_cannot_claim_it(self):
        """``save()``, not only ``clean()`` — nothing else calls full_clean().

        The feature editor, the catalog loader and every fixture reach the
        table through ``save()``. A row that says "hidden" and "show at title"
        must not exist for a later reader to have to reconcile.
        """
        feature = _feature(visibility=OWNER, show_at_title=True, show_as_badge=True)
        feature.refresh_from_db()
        assert (feature.show_at_title, feature.show_as_badge) == (False, False)

    def test_a_public_feature_keeps_its_flags(self):
        feature = _feature(slug="colour", name="Colour", show_at_title=True, show_as_badge=True)
        feature.refresh_from_db()
        assert (feature.show_at_title, feature.show_as_badge) == (True, True)

    def test_turning_a_shown_feature_hidden_clears_the_flags(self):
        feature = _feature(slug="imei", name="IMEI", show_as_badge=True)
        feature.visibility = STAFF
        feature.save()
        feature.refresh_from_db()
        assert feature.show_as_badge is False


# ── the resolved-schema boundaries ───────────────────────────────────


class TestResolvedSchemaCarriesVisibility:
    def test_feature_defs_carries_it(self, category):
        (definition,) = category.feature_defs()
        assert definition["visibility"] == OWNER

    def test_feature_defs_builds_a_featuredef_that_keeps_it(self, category):
        from stapel_attributes.base import FeatureDef

        (definition,) = category.feature_defs()
        assert FeatureDef.from_dict(definition).visibility == OWNER

    def test_get_feature_schema_carries_it(self, category, vin):
        entry = category.get_feature_schema()[str(vin.pk)]
        assert entry["visibility"] == OWNER

    def test_a_public_feature_still_answers_the_key(self, category):
        """Empty, not absent — a consumer must not have to guess the default."""
        colour = _feature(slug="colour", name="Colour", mandatory=False)
        CategoryFeature.objects.create(category=category, feature=colour, order=1)
        definition = next(d for d in category.feature_defs() if d["slug"] == "colour")
        assert definition["visibility"] == PUBLIC


class TestSerializersCarryVisibility:
    def test_feature_serializer(self, vin):
        assert FeatureSerializer(vin).data["visibility"] == OWNER

    def test_feature_compact_serializer(self, vin):
        assert FeatureCompactSerializer(vin).data["visibility"] == OWNER

    def test_the_editor_serializer_defaults_to_public(self):
        serializer = FeatureEditorFeatureSerializer(data={"slug": "colour"})
        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data["visibility"] == PUBLIC

    def test_the_editor_serializer_accepts_a_disclosure_level(self):
        serializer = FeatureEditorFeatureSerializer(data={"slug": "vin", "visibility": OWNER})
        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data["visibility"] == OWNER

    def test_the_editor_serializer_refuses_an_unknown_level(self):
        serializer = FeatureEditorFeatureSerializer(data={"slug": "vin", "visibility": "private"})
        assert not serializer.is_valid()
        assert "visibility" in serializer.errors


class TestTheFeaturesEndpointCarriesVisibility:
    def test_it_reaches_the_wire(self, api_client, category):
        (payload,) = api_client.get(f"{BASE}/categories/{category.pk}/features/").json()
        assert payload["visibility"] == OWNER


# ── the feature editor ───────────────────────────────────────────────


class TestFeatureEditorCarriesVisibility:
    """The writable payload accepts it, so the apply path must store it."""

    @pytest.fixture
    def empty_category(self) -> Category:
        return Category.objects.create(name="Cars", slug="cars", draft="")

    def _apply(self, cat, items):
        revision = Category.objects.values_list("revision", flat=True).get(pk=cat.pk)
        apply_feature_editor_changes(cat, items, base_revision=revision)

    def _payload(self, **overrides):
        payload = {
            "name": "VIN",
            "slug": "vin",
            "config": {"type": "string", "maxLength": 17},
            "visibility": OWNER,
        }
        payload.update(overrides)
        return payload

    def test_create(self, empty_category):
        self._apply(
            empty_category,
            [FeatureEditorItem(action="create", order=0, feature=self._payload())],
        )
        created = Feature.objects.get(slug="vin", tn_parent__isnull=True)
        assert created.visibility == OWNER

    def test_edit(self, empty_category):
        feature = _feature(visibility=PUBLIC)
        CategoryFeature.objects.create(category=empty_category, feature=feature, order=0)
        self._apply(
            empty_category,
            [FeatureEditorItem(action="edit", order=0, feature=self._payload(id=feature.pk))],
        )
        feature.refresh_from_db()
        assert feature.visibility == OWNER

    def test_inherit(self, empty_category):
        root = _feature(visibility=PUBLIC)
        CategoryFeature.objects.create(category=empty_category, feature=root, order=0)
        self._apply(
            empty_category,
            [FeatureEditorItem(action="inherit", order=0, feature=self._payload(id=root.pk))],
        )
        assert Feature.objects.get(tn_parent=root).visibility == OWNER

    def test_an_edit_that_hides_a_badge_clears_the_badge(self, empty_category):
        feature = _feature(show_as_badge=True, visibility=PUBLIC)
        CategoryFeature.objects.create(category=empty_category, feature=feature, order=0)
        self._apply(
            empty_category,
            [
                FeatureEditorItem(
                    action="edit",
                    order=0,
                    feature=self._payload(id=feature.pk, show_as_badge=True),
                )
            ],
        )
        feature.refresh_from_db()
        assert feature.show_as_badge is False


# ── catalog fixtures ─────────────────────────────────────────────────


class TestCatalogFixturesCarryVisibility:
    """Export → import must not launder a hidden field back into public.

    A catalogue is exported to JSON and re-imported into a fresh deployment.
    A record that drops the axis re-creates the VIN as ``public`` there, with
    nothing failing anywhere along the way.
    """

    def test_the_canonical_record_carries_it(self, vin):
        from stapel_categories.catalog_fixtures import _feature_record

        assert _feature_record(vin, include_test=False)["visibility"] == OWNER

    def test_an_inline_override_carries_it(self, vin):
        from stapel_categories.catalog_fixtures import _feature_list_entry

        override = Feature.objects.create(
            tn_parent=vin, slug="vin", name="VIN", config={"type": "string"}, visibility=STAFF
        )
        assert _feature_list_entry(override, include_test=False)["visibility"] == STAFF

    def test_a_load_applies_it(self):
        from stapel_categories.catalog_load import _apply_feature_upsert

        feat = _apply_feature_upsert(
            {"slug": "vin", "name": "VIN", "config": {"type": "string"}, "visibility": OWNER}
        )
        feat.refresh_from_db()
        assert feat.visibility == OWNER

    def test_a_record_that_says_nothing_loads_as_public(self):
        from stapel_categories.catalog_load import _apply_feature_upsert

        feat = _apply_feature_upsert(
            {"slug": "colour", "name": "Colour", "config": {"type": "string"}}
        )
        assert feat.visibility == PUBLIC

    def test_a_contradictory_record_normalizes_the_way_the_row_will(self):
        """Otherwise the dirty guard bumps the revision on every single load.

        ``_apply_feature_upsert`` compares the state it is about to write with
        the state already stored; ``save()`` silences the display flags, so a
        fixture asserting "hidden + shown at title" would never equal the row
        it just wrote.
        """
        from stapel_categories.catalog_load import _normalize_feature_record

        normalized = _normalize_feature_record(
            {
                "slug": "vin",
                "name": "VIN",
                "config": {"type": "string"},
                "visibility": OWNER,
                "show_at_title": True,
                "show_as_badge": True,
            }
        )
        assert normalized["visibility"] == OWNER
        assert normalized["show_at_title"] is False
        assert normalized["show_as_badge"] is False


# ── admin ────────────────────────────────────────────────────────────


class TestFeatureAdminExposesVisibility:
    @pytest.fixture
    def staff_request(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_superuser(
            username="admin", email="admin@test.com", password="pw"
        )
        request = RequestFactory().get("/admin/")
        request.user = user
        return request

    @pytest.fixture
    def feature_admin(self):
        return FeatureAdmin(Feature, AdminSite())

    def test_the_changeform_offers_it(self, feature_admin, staff_request, vin):
        form_class = feature_admin.get_form(staff_request, vin)
        assert "visibility" in form_class.base_fields

    def test_the_form_lists_it_without_relying_on_the_fieldsets(self):
        """``Meta.fields`` and the fieldsets both, deliberately.

        ``ModelAdmin.get_form`` overrides ``Meta.fields`` from the fieldsets,
        so a field named only in the fieldsets still renders in the admin —
        and then silently disappears wherever the form is used directly.
        """
        from stapel_categories.forms import FeatureAdminForm

        assert "visibility" in FeatureAdminForm.Meta.fields
        assert "visibility" in FeatureAdminForm().fields

    def test_it_is_not_filed_under_display_options(self, feature_admin, staff_request):
        """A disclosure decision must not sit next to the badge checkbox."""
        fieldsets = dict(feature_admin.get_fieldsets(staff_request))
        assert "visibility" not in fieldsets["Display Options"]["fields"]
        assert fieldsets["Disclosure"]["fields"] == ("visibility",)

    def test_the_disclosure_help_names_the_re_projection(self, feature_admin, staff_request):
        """Setting the axis is not done until stored values are re-stamped."""
        description = dict(feature_admin.get_fieldsets(staff_request))["Disclosure"]["description"]
        assert "listings_reproject_features" in description

    def test_the_changeform_saves_it(self, feature_admin, staff_request, vin):
        form_class = feature_admin.get_form(staff_request, vin)
        form = form_class(
            {
                "tn_parent": "",
                "translate": "all",
                "slug": vin.slug,
                "name": vin.name,
                "icon": "",
                "comment": "",
                "is_test": False,
                "tn_priority": 0,
                "config": '{"type": "string", "maxLength": 17}',
                "mandatory": True,
                "show_as_badge": False,
                "show_at_title": False,
                "visibility": STAFF,
                "description": "",
                "example": "",
                "default": "null",
                "hints": "[]",
                "group": "",
                "rules": "[]",
            },
            instance=vin,
        )
        assert form.is_valid(), form.errors
        assert form.save().visibility == STAFF
