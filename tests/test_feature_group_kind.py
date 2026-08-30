"""The composite (`group`) kind, at every boundary this module owns.

stapel-attributes 0.6.0 adds one feature type that is not a scalar: `group`
holds a small TABLE — `config.fields` is a list of **full feature
definitions** and the value is a list of rows keyed by child slug. Nothing in
this module knows that; `config` is stored verbatim and validated by the
engine. Which is exactly why the risk lives at the SEAMS rather than in a code
path: a serializer that flattens a nested list, a fixture writer that reorders
the children, a comm function whose schema refuses an object inside an object
— each one loses the composite quietly and nothing here fails.

So this module pins the crossings, not the semantics (those are
stapel-attributes' own suite): the model accepts it and refuses what the
engine refuses, `feature_defs()` and `categories.features` carry the children,
the read serializers carry them, and an export/load round-trip is byte-stable.
"""
import json
import tempfile

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from stapel_core.comm import call

from stapel_categories import catalog_fixtures as cf
from stapel_categories import catalog_load as cl
from stapel_categories.models import Category, CategoryFeature, Feature
from stapel_categories.serializers import FeatureCompactSerializer, FeatureSerializer
from stapel_categories.translation_keys import collect_feature_translation_keys

pytestmark = pytest.mark.django_db

#: Avito's `DiscountLadderList`: "from N units, M % off", up to five steps —
#: the shape 2 468 fields of that corpus carry and no scalar kind could hold.
LADDER_CONFIG = {
    "type": "group",
    "fields": [
        {
            "slug": "quantity",
            "name": "From, units",
            "mandatory": True,
            "config": {"type": "int", "min": 1, "max": 10000000},
            "description": "Discount threshold",
            "example": "100",
        },
        {
            "slug": "discount",
            "name": "Discount",
            "config": {"type": "int", "min": 1, "max": 30, "postfix": "%"},
        },
    ],
    "repeat": {"min": 1, "max": 5},
}


def _ladder(**kwargs) -> Feature:
    payload = {
        "name": "Wholesale discount",
        "slug": "discount_ladder",
        "config": LADDER_CONFIG,
    }
    payload.update(kwargs)
    return Feature.objects.create(**payload)


# ── the model ────────────────────────────────────────────────────────


class TestFeatureCleanAcceptsAComposite:
    def test_a_valid_group_passes(self):
        feature = _ladder()
        feature.clean()
        assert [child["slug"] for child in feature.config["fields"]] == ["quantity", "discount"]
        assert feature.config["repeat"] == {"min": 1, "max": 5}

    def test_a_nested_group_is_refused_on_the_config_field(self):
        """Depth 1 is the engine's rule; this pins that it reaches the admin."""
        feature = _ladder(
            config={
                "type": "group",
                "fields": [{"slug": "inner", "config": LADDER_CONFIG}],
            }
        )
        with pytest.raises(ValidationError) as exc:
            feature.clean()
        assert "config" in exc.value.message_dict
        assert "nesting depth is 1" in str(exc.value)

    def test_a_child_carrying_rules_is_refused(self):
        """A rule inside a row could never fire — `evaluate_rules` reads a flat
        map of TOP-LEVEL slugs — so the engine refuses it rather than accepting
        a rule that silently never matches."""
        feature = _ladder(
            config={
                "type": "group",
                "fields": [
                    {
                        "slug": "quantity",
                        "config": {"type": "int"},
                        "rules": [
                            {"effect": "require", "when": {"all": [
                                {"feature": "wholesale", "op": "filled"}]}}
                        ],
                    }
                ],
            }
        )
        with pytest.raises(ValidationError) as exc:
            feature.clean()
        assert "config" in exc.value.message_dict

    def test_a_rule_on_the_group_itself_is_accepted(self):
        """Conditional behaviour for a composite lives OUTSIDE it."""
        feature = _ladder(
            rules=[{"effect": "require", "when": {"all": [
                {"feature": "wholesale", "op": "in", "values": ["true"]}]}}]
        )
        feature.clean()
        assert feature.rules[0]["effect"] == "require"

    def test_a_broken_child_config_is_reported_on_config(self):
        feature = _ladder(
            config={
                "type": "group",
                "fields": [{"slug": "quantity", "config": {"type": "int", "min": 9, "max": 1}}],
            }
        )
        with pytest.raises(ValidationError) as exc:
            feature.clean()
        assert "config" in exc.value.message_dict


# ── the boundaries a value-validating consumer reads ─────────────────


class TestTheChildrenCrossEveryBoundary:
    @pytest.fixture
    def category(self):
        category = Category.objects.create(name="Wholesale", slug="wholesale")
        CategoryFeature.objects.create(category=category, feature=_ladder(), order=0)
        return category

    def test_feature_defs_carries_the_nested_fields_verbatim(self, category):
        (definition,) = category.feature_defs()
        assert definition["config"] == LADDER_CONFIG
        assert definition["config"]["fields"][0]["mandatory"] is True

    def test_the_comm_function_carries_them_too(self, category):
        """Schema validation is ON in this suite — a `$defs.ResolvedFeature`
        that refused an object inside `config` would fail right here."""
        result = call("categories.features", {"category_id": category.pk})
        (definition,) = result["features"]
        assert definition["config"]["type"] == "group"
        assert [child["slug"] for child in definition["config"]["fields"]] == [
            "quantity",
            "discount",
        ]
        assert definition["config"]["repeat"] == {"min": 1, "max": 5}

    def test_the_payload_still_builds_a_FeatureDef_the_engine_accepts(self, category):
        """The point of the whole crossing: what comes out of the wire has to
        go into the engine unchanged."""
        from stapel_attributes.validation import coerce_feature_defs, validate_dto_structured

        payload = call("categories.features", {"category_id": category.pk})["features"]
        defs = coerce_feature_defs(payload)
        good = {"discount_ladder": {"type": "group", "value": [{"quantity": 10, "discount": 5}]}}
        assert validate_dto_structured(defs, good).valid

        over_cap = {"discount_ladder": {"type": "group", "value": [
            {"quantity": n} for n in range(1, 8)]}}
        result = validate_dto_structured(defs, over_cap)
        assert not result.valid
        assert result.results[0].error.value == "above_maximum"

    @pytest.mark.parametrize("serializer", [FeatureSerializer, FeatureCompactSerializer])
    def test_the_read_serializers_carry_them(self, serializer):
        data = serializer(_ladder()).data
        assert data["config"]["type"] == "group"
        assert len(data["config"]["fields"]) == 2

    def test_translation_keys_aggregate_over_the_children(self):
        """A child is not a catalog row — nothing else walks it — so the
        composite's own `get_translation_keys` is the only place its children's
        names and option labels can be collected from."""
        feature = _ladder(
            config={
                "type": "group",
                "fields": [
                    {"slug": "colour", "name": "feature.colour", "config": {
                        "type": "select",
                        "options": [{"value": "red", "label": "option.red"}],
                    }},
                ],
            }
        )
        category = Category.objects.create(name="Paints", slug="paints")
        CategoryFeature.objects.create(category=category, feature=feature, order=0)
        keys = collect_feature_translation_keys()
        assert "feature.colour" in keys
        assert "option.red" in keys


# ── fixtures: what the Avito importer writes and `load_catalog` reads ─


class TestCatalogFixturesRoundTripAComposite:
    def test_export_load_export_is_byte_identical(self):
        category = Category.objects.create(name="Wholesale", slug="wholesale")
        CategoryFeature.objects.create(category=category, feature=_ladder(), order=0)

        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            call_command("export_catalog", out=first)
            features = (
                json.loads((__import__("pathlib").Path(first) / cf.FEATURES_FILE).read_text())
            )
            (record,) = [f for f in features if f["slug"] == "discount_ladder"]
            assert record["config"] == LADDER_CONFIG

            Feature.objects.all().delete()
            Category.objects.all().delete()
            report = cl.load_catalog(first, seed_if_empty=True)
            assert not report.failed

            reloaded = Feature.objects.get(slug="discount_ladder")
            assert reloaded.config == LADDER_CONFIG

            call_command("export_catalog", out=second)
            assert (
                (__import__("pathlib").Path(second) / cf.FEATURES_FILE).read_bytes()
                == (__import__("pathlib").Path(first) / cf.FEATURES_FILE).read_bytes()
            ), "features.json drifted across a round-trip of a composite"
