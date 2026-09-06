"""Which feature of a leaf is the make, the model, the year.

Four things are pinned here, against each other:

* the RULE TABLE — what a slug derives, and the suffix rule that keeps
  ``make_ref_select`` from being the spelling nobody added;
* the DERIVATION — that the roles land on a cars leaf, that an ambiguous leaf
  derives nothing anywhere and says so, and that an authored role wins;
* what a READER is told: the features API, ``feature_defs`` and the comm
  Function all carry it, and a catalogue that never heard of the field
  answers exactly what it answered before;
* the FIXTURE round trip, since a role that does not survive it is a decision
  the next image forgets.
"""
import io
import tempfile

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command

from stapel_attributes.axis import AXIS_ROLES, by_axis_role
from stapel_categories import catalog_fixtures as cf
from stapel_categories import catalog_load as cl
from stapel_categories.axis_roles import (
    AXIS_ROLE_BY_SLUG,
    AXIS_ROLE_TIER_BY_SLUG,
    derive_axis_roles,
    find_ambiguities,
    precedence_for_slug,
    role_for_slug,
)
from stapel_categories.models import Category, CategoryFeature, Feature

from .test_catalog_load import _export, _read_json, _wipe_db, _write_json

pytestmark = pytest.mark.django_db

BASE = "/catalog/api"


def feature(slug: str, **kwargs) -> Feature:
    return Feature.objects.create(
        name=kwargs.pop("name", f"feature.{slug}"),
        slug=slug,
        config=kwargs.pop("config", {"type": "string"}),
        **kwargs,
    )


def leaf(slug: str, *feature_slugs: str, parent=None) -> Category:
    """A category linking exactly *feature_slugs* (creating them as needed)."""
    category = Category.objects.create(name=slug.title(), slug=slug, tn_parent=parent)
    CategoryFeature.objects.filter(category=category).delete()
    for order, feature_slug in enumerate(feature_slugs):
        row = Feature.objects.filter(slug=feature_slug).first() or feature(feature_slug)
        CategoryFeature.objects.create(category=category, feature=row, order=order)
    return category


def cars_leaf() -> Category:
    """The live shape: a vocabulary-backed make, a model, a year, a mileage."""
    return leaf(
        "legkovye-s-probegom",
        "make_ref_select",
        "model_ref_select",
        "generation",
        "god_vypuska",
        "kilometrage",
        "color",
    )


class TestTheRuleTable:
    def test_the_vocabulary_is_the_canon_s(self):
        """A role this module invents is a role no consumer switches on."""
        assert set(AXIS_ROLE_BY_SLUG.values()) <= set(AXIS_ROLES)

    def test_the_model_choices_mirror_the_canon(self):
        # Pinned rather than built at import time, so a reordered or renamed
        # upstream constant fails loudly here instead of migrating silently.
        assert tuple(value for value, _ in Feature.AxisRole.choices) == AXIS_ROLES

    @pytest.mark.parametrize("slug, role", [
        ("brand", "make"),
        ("make", "make"),
        ("vendor", "make"),
        ("manufacturer", "make"),
        ("model", "model"),
        ("generation", "generation"),
        ("year", "year"),
        ("god_vypuska", "year"),
        ("mileage", "mileage"),
        ("kilometrage", "mileage"),
    ])
    def test_every_documented_spelling(self, slug, role):
        assert role_for_slug(slug) == role

    @pytest.mark.parametrize("slug, role", [
        ("make_ref_select", "make"),
        ("model_ref_select", "model"),
        ("generation_ref_select", "generation"),
        ("Make_Ref_Select", "make"),
        ("brand_select", "make"),
    ])
    def test_the_suffix_rule_covers_the_vocabulary_backed_spellings(self, slug, role):
        # The 2026-09-05 import renamed five features across exactly this
        # seam; listing each variant by hand is how the fourth one is missed.
        assert role_for_slug(slug) == role

    @pytest.mark.parametrize("slug", [
        "color", "brand_new", "modelling", "yearly", "", None, "_ref_select",
    ])
    def test_a_near_miss_derives_nothing(self, slug):
        # A wrongly stamped make is worse than an unstamped feature.
        assert role_for_slug(slug) is None


class TestPrecedence:
    """Which spelling a catalogue MEANS when a leaf carries several."""

    def test_every_table_word_has_a_tier(self):
        # A spelling added to the rule table with no tier would silently land
        # in the last one — findable here, not in a storefront's link.
        assert set(AXIS_ROLE_BY_SLUG) == set(AXIS_ROLE_TIER_BY_SLUG)

    @pytest.mark.parametrize("stronger, weaker", [
        ("make", "make_ref_select"),
        ("make_ref_select", "vendor"),
        ("make_ref_select", "manufacturer"),
        ("vendor", "brand"),
        ("manufacturer", "brand"),
        ("model", "model_ref_select"),
        ("year", "god_vypuska"),
        ("mileage", "kilometrage"),
    ])
    def test_the_documented_order(self, stronger, weaker):
        assert precedence_for_slug(stronger) < precedence_for_slug(weaker)

    def test_two_words_of_one_tier_are_a_tie(self):
        assert precedence_for_slug("vendor") == precedence_for_slug("manufacturer")

    def test_a_slug_outside_the_table_claims_nothing(self):
        assert precedence_for_slug("color") is None

    def test_the_live_leaf_prefers_the_catalogue_s_own_make(self):
        """The 82 leaves that carry a general `brand` beside `make_ref_select`."""
        category = leaf("telefony", "brand", "make_ref_select", "model_ref_select")

        decided, ambiguities = derive_axis_roles(apply=True)

        assert ambiguities == []
        assert decided["make_ref_select"] == "make"
        assert "brand" not in decided
        defs = Category.objects.get(pk=category.pk).feature_defs()
        assert by_axis_role(defs)["make"]["slug"] == "make_ref_select"

    def test_a_per_category_row_keeps_its_role_where_it_stands_alone(self):
        # `brand` is «Бренд одежды» on its own leaf and a second word for the
        # make on the leaf that also carries `make_ref_select`. Per-category
        # rows can hold both answers, so both leaves name exactly one make.
        leaf("odezhda", "brand")
        root = Feature.objects.get(slug="brand")
        override = Feature.objects.create(
            tn_parent=root, slug="brand", name=root.name, config={"type": "string"},
        )
        cars = leaf("legkovye", "make_ref_select")
        CategoryFeature.objects.create(category=cars, feature=override, order=1)

        _, ambiguities = derive_axis_roles(apply=True)

        assert ambiguities == []
        assert Feature.objects.get(pk=root.pk).resolved_axis_role == "make"
        assert Feature.objects.get(pk=override.pk).resolved_axis_role is None
        assert Feature.objects.get(slug="make_ref_select").resolved_axis_role == "make"

    def test_a_shared_row_that_cannot_hold_both_answers_is_blanked(self):
        # One row, two categories wanting opposite answers of it: precedence
        # settles the leaf, the column cannot, so nobody is stamped and the
        # leaf where it lost is reported — the honest degradation.
        leaf("odezhda", "brand")
        cars = leaf("legkovye", "make_ref_select")
        CategoryFeature.objects.create(
            category=cars, feature=Feature.objects.get(slug="brand"), order=1,
        )

        decided, ambiguities = derive_axis_roles(apply=True)

        assert "brand" not in decided
        assert [(a.category, a.role, a.slugs) for a in ambiguities] == [
            ("legkovye", "make", ("brand", "make_ref_select")),
        ]


class TestResolvedValue:
    def test_a_plain_feature_has_no_axis(self):
        assert feature("color").resolved_axis_role is None

    def test_the_derivation_cache_answers_a_blank_authored_column(self):
        row = feature("make", axis_role_derived="make")

        assert row.resolved_axis_role == "make"

    def test_an_authored_role_wins_outright(self):
        row = feature("make", axis_role="model", axis_role_derived="make")

        assert row.resolved_axis_role == "model"

    def test_an_unknown_role_is_refused_at_the_door(self):
        unsaved = Feature(
            name="Make", slug="make", config={"type": "string"},
            axis_role="manufacturer",
        )
        with pytest.raises(ValidationError) as exc:
            unsaved.clean()
        assert "axis_role" in exc.value.message_dict

    def test_save_refuses_it_too(self):
        # save() is the path the loader and the feature editor take; neither
        # calls full_clean on every row.
        with pytest.raises(ValueError):
            feature("make", axis_role="brand")


class TestDerivation:
    def test_the_roles_land_on_a_cars_leaf(self):
        cars_leaf()

        decided, ambiguities = derive_axis_roles(apply=True)

        assert ambiguities == []
        assert decided == {
            "make_ref_select": "make",
            "model_ref_select": "model",
            "generation": "generation",
            "god_vypuska": "year",
            "kilometrage": "mileage",
        }
        assert Feature.objects.get(slug="make_ref_select").resolved_axis_role == "make"
        assert Feature.objects.get(slug="color").resolved_axis_role is None

    def test_it_writes_only_the_derivation_column(self):
        cars_leaf()

        derive_axis_roles(apply=True)

        row = Feature.objects.get(slug="make_ref_select")
        assert row.axis_role == ""
        assert row.axis_role_derived == "make"

    def test_a_dry_derivation_writes_nothing(self):
        cars_leaf()

        decided, _ = derive_axis_roles(apply=False)

        assert decided["make_ref_select"] == "make"
        assert Feature.objects.get(slug="make_ref_select").axis_role_derived == ""

    def test_it_is_idempotent(self):
        cars_leaf()
        derive_axis_roles(apply=True)

        again, _ = derive_axis_roles(apply=True)

        assert again["make_ref_select"] == "make"
        assert Feature.objects.get(slug="make_ref_select").axis_role_derived == "make"

    def test_a_role_that_stops_applying_is_blanked(self):
        cars_leaf()
        derive_axis_roles(apply=True)
        Feature.objects.filter(slug="color").update(axis_role_derived="make")

        derive_axis_roles(apply=True)

        assert Feature.objects.get(slug="color").axis_role_derived == ""

    def test_an_inherited_feature_counts(self):
        parent = leaf("transport", "make")
        leaf("legkovye", "model", parent=parent)

        decided, ambiguities = derive_axis_roles(apply=True)

        assert ambiguities == []
        assert decided == {"make": "make", "model": "model"}


class TestAmbiguity:
    def test_a_tie_inside_one_tier_derives_neither(self):
        # `vendor` and `manufacturer` sit in the same precedence tier, so
        # nothing in the catalogue says which of them the axis is.
        leaf("odezhda", "vendor", "manufacturer", "model")

        decided, ambiguities = derive_axis_roles(apply=True)

        assert "vendor" not in decided and "manufacturer" not in decided
        assert decided == {"model": "model"}
        assert Feature.objects.get(slug="vendor").resolved_axis_role is None
        assert Feature.objects.get(slug="manufacturer").resolved_axis_role is None

    def test_the_ambiguity_names_the_category_the_role_and_both_slugs(self):
        leaf("odezhda", "manufacturer", "vendor")

        _, ambiguities = derive_axis_roles(apply=True)

        assert len(ambiguities) == 1
        assert ambiguities[0].category == "odezhda"
        assert ambiguities[0].role == "make"
        assert ambiguities[0].slugs == ("manufacturer", "vendor")
        assert "odezhda" in str(ambiguities[0])

    def test_one_ambiguous_leaf_blocks_the_slug_everywhere(self):
        # The Feature row is shared, so a role that is wrong in one leaf
        # cannot be right on the row.
        leaf("odezhda", "vendor", "manufacturer")
        leaf("obuv", "vendor")

        decided, _ = derive_axis_roles(apply=True)

        assert "vendor" not in decided

    def test_an_inherited_clash_is_still_a_clash(self):
        parent = leaf("transport", "vendor")
        leaf("legkovye", "make", parent=parent)

        _, ambiguities = derive_axis_roles(apply=True)

        assert [a.category for a in ambiguities] == ["legkovye"]

    def test_an_authored_role_settles_it_for_the_reader(self):
        leaf("odezhda", "vendor", "manufacturer")
        derive_axis_roles(apply=True)

        Feature.objects.filter(slug="vendor").update(axis_role="make")

        assert Feature.objects.get(slug="vendor").resolved_axis_role == "make"
        assert Feature.objects.get(slug="manufacturer").resolved_axis_role is None

    def test_find_ambiguities_answers_the_same_thing_on_its_own(self):
        leaf("odezhda", "vendor", "manufacturer")

        assert find_ambiguities() == derive_axis_roles(apply=False)[1]

    def test_a_clean_catalogue_reports_none(self):
        cars_leaf()

        assert find_ambiguities() == []


class TestWhatAReaderIsTold:
    def test_feature_defs_carry_the_resolved_role(self):
        category = cars_leaf()
        derive_axis_roles(apply=True)

        defs = Category.objects.get(pk=category.pk).feature_defs()

        assert by_axis_role(defs)["make"]["slug"] == "make_ref_select"

    def test_a_catalogue_with_no_axes_answers_null_everywhere(self):
        """The whole point of the default: nothing that omitted it changes."""
        category = leaf("knigi", "color", "size")
        derive_axis_roles(apply=True)

        defs = Category.objects.get(pk=category.pk).feature_defs()

        assert [d["axis_role"] for d in defs] == [None, None]
        assert by_axis_role(defs) == {}

    def test_the_feature_schema_carries_it_too(self):
        category = cars_leaf()
        derive_axis_roles(apply=True)

        schema = Category.objects.get(pk=category.pk).get_feature_schema()

        roles = {row["slug"]: row["axis_role"] for row in schema.values()}
        assert roles["make_ref_select"] == "make"
        assert roles["color"] is None

    def test_the_features_api_carries_it(self, api_client):
        category = cars_leaf()
        derive_axis_roles(apply=True)

        response = api_client.get(f"{BASE}/categories/{category.pk}/features/")

        assert response.status_code == 200
        roles = {row["slug"]: row["axis_role"] for row in response.data}
        assert roles["make_ref_select"] == "make"
        assert roles["god_vypuska"] == "year"
        assert roles["color"] is None

    def test_the_features_api_answers_null_on_an_ambiguous_leaf(self, api_client):
        category = leaf("odezhda", "vendor", "manufacturer")
        derive_axis_roles(apply=True)

        response = api_client.get(f"{BASE}/categories/{category.pk}/features/")

        assert {row["axis_role"] for row in response.data} == {None}

    def test_the_comm_function_carries_it(self):
        from stapel_core.comm import call

        category = cars_leaf()
        derive_axis_roles(apply=True)

        result = call("categories.features", {"category_id": category.pk})

        assert by_axis_role(result["features"])["make"]["slug"] == "make_ref_select"

    def test_the_detail_serializer_separates_authored_from_derived(self, api_client):
        cars_leaf()
        derive_axis_roles(apply=True)
        row = Feature.objects.get(slug="model_ref_select")
        row.axis_role = "generation"
        row.save()

        response = api_client.get(f"{BASE}/features/{row.pk}/")

        assert response.status_code == 200
        assert response.data["axis_role"] == "generation"
        assert response.data["axis_role_authored"] == "generation"
        assert response.data["axis_role_derived"] == "model"


class TestTheFixtureRoundTrip:
    def test_an_authored_role_survives_export_and_load(self):
        cars_leaf()
        Feature.objects.filter(slug="make_ref_select").update(axis_role="make")

        with tempfile.TemporaryDirectory() as out:
            _export(out)
            records = _read_json(out, cf.FEATURES_FILE)
            assert next(
                r for r in records if r["slug"] == "make_ref_select"
            )["axis_role"] == "make"

            _wipe_db()
            cl.load_catalog(out, seed_if_empty=True)

        assert Feature.objects.get(slug="make_ref_select").axis_role == "make"

    def test_a_derived_role_never_travels(self):
        """It is a cache of one run's guess, not canon."""
        cars_leaf()
        derive_axis_roles(apply=True)

        with tempfile.TemporaryDirectory() as out:
            _export(out)
            records = _read_json(out, cf.FEATURES_FILE)

        assert all("axis_role" not in r for r in records)

    def test_a_record_without_the_key_hashes_as_it_always_did(self):
        """No sidecar regeneration, no re-plan, on a catalogue that says nothing."""
        cars_leaf()

        with tempfile.TemporaryDirectory() as out:
            _export(out)
            before = _read_json(out, cf.STATE_FILE)["features"]
            report = cl.load_catalog(out)

        skipped = [it for it in report.features if it.kind == cl.SKIPPED]
        assert len(skipped) == len(before)

    def test_the_loader_derives_after_applying(self):
        cars_leaf()
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            _wipe_db()
            report = cl.load_catalog(out, seed_if_empty=True)

        assert report.axis_roles["make_ref_select"] == "make"
        assert report.axis_role_ambiguities == []
        assert Feature.objects.get(slug="make_ref_select").axis_role_derived == "make"

    def test_the_loader_reports_an_ambiguity_it_refused(self):
        leaf("odezhda", "vendor", "manufacturer")
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            report = cl.load_catalog(out)

        assert [a.role for a in report.axis_role_ambiguities] == ["make"]

    def test_an_authored_role_on_an_override_survives_the_round_trip(self):
        parent = leaf("transport", "model")
        child = Category.objects.create(name="Cars", slug="cars", tn_parent=parent)
        root = Feature.objects.get(slug="model")
        override = Feature.objects.create(
            tn_parent=root, slug="model", name=root.name,
            config={"type": "string"}, axis_role="generation",
        )
        CategoryFeature.objects.filter(category=child).delete()
        CategoryFeature.objects.create(category=child, feature=override, order=0)

        with tempfile.TemporaryDirectory() as out:
            _export(out)
            categories = _read_json(out, cf.CATEGORIES_FILE)
            entry = next(c for c in categories if c["slug"] == "cars")["features"][0]
            assert entry["axis_role"] == "generation"

            _wipe_db()
            cl.load_catalog(out, seed_if_empty=True)

        reloaded = Category.objects.get(slug="cars").get_all_features()
        assert [f.resolved_axis_role for f in reloaded] == ["generation"]

    @pytest.mark.parametrize("on_conflict", ["fixture-wins", "db-wins"])
    def test_an_authored_role_survives_a_load_that_never_mentions_it(self, on_conflict):
        """The 2026-09-07 stand incident, both policies.

        An operator ran `set_axis_role`, then the next `load_catalog
        --on-conflict fixture-wins` over a fixture that had never heard of the
        field wiped it. An absent key is not the instruction "blank it".
        """
        cars_leaf()

        with tempfile.TemporaryDirectory() as out:
            _export(out)
            records = _read_json(out, cf.FEATURES_FILE)
            for record in records:
                record["comment"] = "moved on the fixture side"
            _write_json(out, cf.FEATURES_FILE, records)
            # Authored AFTER the export: exactly the operator's order.
            Feature.objects.filter(slug="make_ref_select").update(axis_role="make")
            cl.load_catalog(out, on_conflict=on_conflict)

        row = Feature.objects.get(slug="make_ref_select")
        assert row.axis_role == "make"
        if on_conflict == "fixture-wins":
            # …and the rest of the record still applied: the guard is about
            # the one key the fixture did not state, not about the record.
            assert row.comment == "moved on the fixture side"

    def test_a_fixture_that_states_a_role_applies_it(self):
        """What it states IS canon — absence is the only thing that is not."""
        cars_leaf()
        Feature.objects.filter(slug="make_ref_select").update(axis_role="model")

        with tempfile.TemporaryDirectory() as out:
            _export(out)
            records = _read_json(out, cf.FEATURES_FILE)
            for record in records:
                if record["slug"] == "make_ref_select":
                    record["axis_role"] = "make"
            _write_json(out, cf.FEATURES_FILE, records)
            cl.load_catalog(out, on_conflict="fixture-wins")

        assert Feature.objects.get(slug="make_ref_select").axis_role == "make"

    def _load_with_a_nulled_role(self, **kwargs):
        cars_leaf()
        Feature.objects.filter(slug="make_ref_select").update(axis_role="make")
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            records = _read_json(out, cf.FEATURES_FILE)
            for record in records:
                if record["slug"] == "make_ref_select":
                    record["axis_role"] = None
            _write_json(out, cf.FEATURES_FILE, records)
            cl.load_catalog(out, on_conflict="fixture-wins", **kwargs)
        return Feature.objects.get(slug="make_ref_select")

    def test_a_stated_null_is_a_no_op_without_the_flag(self):
        """Erasure is the one instruction that must be typed out loud."""
        assert self._load_with_a_nulled_role().axis_role == "make"

    def test_a_stated_null_erases_with_the_flag(self):
        assert self._load_with_a_nulled_role(clear_axis_role=True).axis_role == ""

    def test_the_command_carries_the_flag(self):
        cars_leaf()
        Feature.objects.filter(slug="make_ref_select").update(axis_role="make")
        with tempfile.TemporaryDirectory() as out:
            _export(out)
            records = _read_json(out, cf.FEATURES_FILE)
            for record in records:
                record["axis_role"] = None
            _write_json(out, cf.FEATURES_FILE, records)
            call_command(
                "load_catalog", dir=out, on_conflict="fixture-wins",
                clear_axis_role=True, stdout=io.StringIO(),
            )

        assert Feature.objects.get(slug="make_ref_select").axis_role == ""

    def test_an_authored_override_role_survives_a_load_that_drops_the_key(self):
        parent = leaf("transport", "model")
        child = Category.objects.create(name="Cars", slug="cars", tn_parent=parent)
        root = Feature.objects.get(slug="model")
        override = Feature.objects.create(
            tn_parent=root, slug="model", name=root.name,
            config={"type": "string"}, axis_role="generation",
        )
        CategoryFeature.objects.filter(category=child).delete()
        CategoryFeature.objects.create(category=child, feature=override, order=0)

        with tempfile.TemporaryDirectory() as out:
            _export(out)
            categories = _read_json(out, cf.CATEGORIES_FILE)
            for record in categories:
                for entry in record.get("features", []):
                    entry.pop("axis_role", None)
                record["comment"] = "moved on the fixture side"
            _write_json(out, cf.CATEGORIES_FILE, categories)
            cl.load_catalog(out, on_conflict="fixture-wins")

        assert Feature.objects.get(pk=override.pk).axis_role == "generation"


class TestSetAxisRoleCommand:
    def _run(self, **kwargs):
        out = io.StringIO()
        call_command("set_axis_role", stdout=out, **kwargs)
        return out.getvalue()

    def test_it_pins_a_role_the_table_never_heard_of(self):
        leaf("stanki", "proizvoditel")

        output = self._run(slug=["proizvoditel"], role="make")

        assert Feature.objects.get(slug="proizvoditel").axis_role == "make"
        assert "Wrote 1 row" in output

    def test_the_pin_outlives_a_re_derivation(self):
        leaf("odezhda", "vendor", "manufacturer")
        self._run(slug=["vendor"], role="make")

        derive_axis_roles(apply=True)

        assert Feature.objects.get(slug="vendor").resolved_axis_role == "make"

    def test_it_is_idempotent(self):
        leaf("stanki", "proizvoditel")
        self._run(slug=["proizvoditel"], role="make")

        output = self._run(slug=["proizvoditel"], role="make")

        assert "Nothing to write" in output

    def test_clear_hands_the_feature_back_to_the_derivation(self):
        cars_leaf()
        derive_axis_roles(apply=True)
        self._run(slug=["make_ref_select"], role="model")

        self._run(slug=["make_ref_select"], clear=True)

        row = Feature.objects.get(slug="make_ref_select")
        assert row.axis_role == ""
        assert row.resolved_axis_role == "make"

    def test_a_dry_run_writes_nothing(self):
        leaf("stanki", "proizvoditel")

        output = self._run(slug=["proizvoditel"], role="make", dry_run=True)

        assert "Dry run" in output
        assert Feature.objects.get(slug="proizvoditel").axis_role == ""

    def test_it_writes_every_row_under_the_slug(self):
        # Root and per-category override alike: the role is a fact about the
        # FIELD, and a stamped root beside an unstamped override is the hole.
        root = feature("vendor")
        Feature.objects.create(
            tn_parent=root, slug="vendor", name=root.name, config={"type": "string"}
        )

        self._run(slug=["vendor"], role="make")

        assert list(
            Feature.objects.filter(slug="vendor").values_list("axis_role", flat=True)
        ) == ["make", "make"]

    def test_a_typo_stops_the_whole_session(self):
        from django.core.management.base import CommandError

        leaf("stanki", "proizvoditel")

        with pytest.raises(CommandError):
            self._run(slug=["proizvoditel", "nosuchfeature"], role="make")

        assert Feature.objects.get(slug="proizvoditel").axis_role == ""


class TestCatalogHealth:
    def test_it_warns_about_an_ambiguity_without_failing(self):
        leaf("odezhda", "vendor", "manufacturer")
        out = io.StringIO()

        call_command("catalog_health", stdout=out)

        assert "axis-role ambiguit" in out.getvalue()
        assert "odezhda" in out.getvalue()

    def test_a_clean_catalogue_says_so(self):
        cars_leaf()
        out = io.StringIO()

        call_command("catalog_health", stdout=out)

        assert "0 axis-role ambiguities" in out.getvalue()
