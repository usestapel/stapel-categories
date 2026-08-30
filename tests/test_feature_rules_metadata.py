"""Rules + form metadata on ``Feature``, and every seam they have to cross.

stapel-attributes 0.5.0 moved requiredness and visibility out of the static
``mandatory`` flag and into ``rules``, and added the form metadata a field
needs to render itself (``description`` / ``example`` / ``default`` /
``hints`` / ``group``). This module stores those six on the model — so the
only thing that can go wrong here is a *boundary* that keeps quiet about
them: a serializer field list, ``feature_defs()``, the feature editor, the
admin. Nothing fails when one of those drops a field; the answer just gets
smaller, which is why each crossing is pinned separately below.
"""
import pytest
from django.contrib.admin.sites import AdminSite
from django.core.exceptions import ValidationError
from django.test import RequestFactory
from stapel_attributes.vocabularies import register_vocabulary_resolver

from stapel_categories.admin import FeatureAdmin
from stapel_categories.feature_editor import FeatureEditorItem, apply_feature_editor_changes
from stapel_categories.models import Category, CategoryFeature, Feature
from stapel_categories.serializers import (
    CategorySerializer,
    FeatureBulkSerializer,
    FeatureCompactSerializer,
    FeatureCreateUpdateSerializer,
    FeatureSerializer,
)
from stapel_categories.translation_keys import collect_feature_translation_keys
from stapel_categories.validators import feature_warnings

from .fake_vocabulary import VOCABULARY, FakeVocabularyResolver

pytestmark = pytest.mark.django_db

BASE = "/catalog/api"

#: The six fields slice S3 adds to every feature-carrying boundary.
NEW_FIELDS = ("rules", "description", "example", "default", "hints", "group")

CONDITION_RULES = [
    {
        "effect": "require",
        "when": {"all": [{"feature": "condition", "op": "in", "values": ["used"]}]},
    }
]


@pytest.fixture
def vocabulary_resolver():
    """Register the fake for the test, then clear it.

    The registry is process-global: a resolver left behind would make a later
    test's ``ref_select`` config validate for the wrong reason.
    """
    register_vocabulary_resolver(FakeVocabularyResolver())
    yield
    register_vocabulary_resolver(None)


def _feature(**kwargs) -> Feature:
    payload = {
        "name": "Screen condition",
        "slug": "screen_condition",
        "config": {"type": "string", "maxLength": 100},
    }
    payload.update(kwargs)
    return Feature.objects.create(**payload)


@pytest.fixture
def rich_feature() -> Feature:
    """One feature carrying a value in all six new fields."""
    return _feature(
        rules=CONDITION_RULES,
        description="feature.screen_condition.help",
        example="No scratches",
        default=["intact"],
        hints=[{"title": "hint.title", "content": "hint.content"}],
        group="About the condition",
    )


# ── model validation ─────────────────────────────────────────────────


class TestFeatureCleanValidatesRules:
    def test_a_valid_rule_set_passes(self):
        feature = _feature(rules=CONDITION_RULES)
        feature.clean()

    def test_a_broken_grammar_is_reported_on_the_rules_field(self):
        feature = _feature(rules=[{"effect": "teleport", "when": {"all": []}}])
        with pytest.raises(ValidationError) as exc:
            feature.clean()
        assert "rules" in exc.value.message_dict
        assert "config" not in exc.value.message_dict

    def test_a_nested_connective_is_rejected(self):
        """The grammar is closed — no nesting, so this must not slip through."""
        feature = _feature(
            rules=[{"effect": "show", "when": {"all": [{"any": [{"feature": "a", "op": "filled"}]}]}}]
        )
        with pytest.raises(ValidationError) as exc:
            feature.clean()
        assert "rules" in exc.value.message_dict

    def test_no_rules_is_the_unchanged_case(self):
        feature = _feature()
        feature.clean()
        assert feature.rules == []


class TestFeatureCleanValidatesHints:
    @pytest.mark.parametrize(
        "hints",
        [
            "not a list",
            ["not an object"],
            [{"title": "t"}],
            [{"title": "t", "content": "c", "extra": "x"}],
            [{"title": "t", "content": 7}],
        ],
    )
    def test_a_malformed_hint_is_reported_on_the_hints_field(self, hints):
        feature = _feature(hints=hints)
        with pytest.raises(ValidationError) as exc:
            feature.clean()
        assert "hints" in exc.value.message_dict

    def test_a_well_formed_hint_list_passes(self):
        feature = _feature(hints=[{"title": "t", "content": "c"}])
        feature.clean()

    def test_an_empty_hint_list_passes(self):
        _feature(hints=[]).clean()


# ── the resolved-schema boundaries ───────────────────────────────────


class TestResolvedSchemaCarriesTheNewFields:
    @pytest.fixture
    def category(self, rich_feature):
        category = Category.objects.create(name="Phones", slug="phones")
        CategoryFeature.objects.create(category=category, feature=rich_feature, order=0)
        return category

    def test_feature_defs_carries_all_six(self, category, rich_feature):
        (definition,) = category.feature_defs()
        assert definition["rules"] == CONDITION_RULES
        assert definition["description"] == "feature.screen_condition.help"
        assert definition["example"] == "No scratches"
        assert definition["default"] == ["intact"]
        assert definition["hints"] == [{"title": "hint.title", "content": "hint.content"}]
        assert definition["group"] == "About the condition"

    def test_feature_defs_builds_a_featuredef_without_loss(self, category):
        """The payload's whole point: a consumer rebuilds FeatureDef from it."""
        from stapel_attributes.base import FeatureDef

        (definition,) = category.feature_defs()
        rebuilt = FeatureDef.from_dict(definition)
        assert rebuilt.rules == CONDITION_RULES
        assert rebuilt.default == ["intact"]
        assert rebuilt.group == "About the condition"

    def test_get_feature_schema_carries_all_six(self, category, rich_feature):
        entry = category.get_feature_schema()[str(rich_feature.pk)]
        for field in NEW_FIELDS:
            assert field in entry, f"get_feature_schema() dropped {field}"
        assert entry["rules"] == CONDITION_RULES

    def test_a_bare_feature_still_answers_every_key(self, category):
        """Empty, not absent — a consumer must not have to guess a default."""
        bare = _feature(slug="colour", name="Colour")
        CategoryFeature.objects.create(category=category, feature=bare, order=1)
        definition = next(d for d in category.feature_defs() if d["slug"] == "colour")
        assert definition["rules"] == []
        assert definition["hints"] == []
        assert definition["description"] == ""
        assert definition["example"] == ""
        assert definition["group"] == ""
        assert definition["default"] is None


class TestSerializersCarryTheNewFields:
    def test_feature_serializer(self, rich_feature):
        data = FeatureSerializer(rich_feature).data
        for field in NEW_FIELDS:
            assert field in data, f"FeatureSerializer dropped {field}"
        assert data["rules"] == CONDITION_RULES

    def test_feature_compact_serializer(self, rich_feature):
        data = FeatureCompactSerializer(rich_feature).data
        for field in NEW_FIELDS:
            assert field in data, f"FeatureCompactSerializer dropped {field}"

    @pytest.mark.parametrize(
        "serializer_class", [FeatureBulkSerializer, FeatureCreateUpdateSerializer]
    )
    def test_the_all_fields_serializers_emit_json_not_strings(
        self, serializer_class, rich_feature
    ):
        """``fields = "__all__"`` picks them up — but only as real JSON.

        A JSONField rendered as its ``repr`` is the failure this pins: it
        round-trips through DRF looking fine and arrives at the consumer as
        an unparseable string.
        """
        data = serializer_class(rich_feature).data
        assert data["rules"] == CONDITION_RULES
        assert isinstance(data["rules"], list)
        assert isinstance(data["hints"], list)
        assert isinstance(data["hints"][0], dict)
        assert data["default"] == ["intact"]

    def test_the_all_fields_serializers_accept_json_on_the_way_in(self):
        serializer = FeatureCreateUpdateSerializer(
            data={
                "name": "Condition",
                "slug": "condition",
                "config": {"type": "string"},
                "rules": CONDITION_RULES,
                "hints": [{"title": "t", "content": "c"}],
                "default": ["used"],
                "group": "Basics",
            }
        )
        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data["rules"] == CONDITION_RULES
        assert serializer.validated_data["default"] == ["used"]

    def test_category_serializer_carries_external_id(self):
        category = Category.objects.create(name="Phones", slug="phones", external_id="129639")
        assert CategorySerializer(category).data["external_id"] == "129639"


# ── HTTP surface ─────────────────────────────────────────────────────


class TestCategoryFeaturesEndpoint:
    def test_a_ref_config_is_served_verbatim(self, api_client, vocabulary_resolver):
        """No inlining of vocabularies at the endpoint (spec D8).

        The phone catalogue this points at holds 14 962 models; the delivery
        contract is that ``/features`` hands back the *pointer* and the client
        resolves terms against the vocabulary service. An endpoint that helpfully
        expanded ``optionsRef`` into ``options`` would be correct-looking and
        unshippable.
        """
        config = {
            "type": "ref_select",
            "optionsRef": {"vocabulary": VOCABULARY, "level": "Model", "parentFeature": "vendor"},
            "maxSelected": 1,
        }
        feature = _feature(name="Model", slug="model", config=config)
        category = Category.objects.create(name="Phones", slug="phones")
        CategoryFeature.objects.create(category=category, feature=feature, order=0)

        response = api_client.get(f"{BASE}/categories/{category.pk}/features/")
        assert response.status_code == 200
        (payload,) = response.json()
        assert payload["config"] == config
        assert "options" not in payload["config"]
        assert payload["config"]["optionsRef"]["vocabulary"] == VOCABULARY

    def test_the_new_fields_reach_the_wire(self, api_client, rich_feature):
        category = Category.objects.create(name="Phones", slug="phones")
        CategoryFeature.objects.create(category=category, feature=rich_feature, order=0)
        (payload,) = api_client.get(f"{BASE}/categories/{category.pk}/features/").json()
        for field in NEW_FIELDS:
            assert field in payload, f"the features endpoint dropped {field}"


class TestValidateDtoHonoursRules:
    """The endpoint's requiredness must come from the rule state, not ``mandatory``."""

    @pytest.fixture
    def category(self):
        category = Category.objects.create(name="Phones", slug="phones")
        condition = _feature(
            name="Condition",
            slug="condition",
            config={
                "type": "select",
                "maxSelected": 1,
                "options": [
                    {"value": "new", "label": "New"},
                    {"value": "used", "label": "Used"},
                ],
            },
        )
        # Statically mandatory, but only shown for a used device.
        screen = _feature(
            name="Screen condition",
            slug="screen_condition",
            config={"type": "string", "maxLength": 100},
            mandatory=True,
            rules=[
                {
                    "effect": "show",
                    "when": {"all": [{"feature": "condition", "op": "in", "values": ["used"]}]},
                }
            ],
        )
        CategoryFeature.objects.create(category=category, feature=condition, order=0)
        CategoryFeature.objects.create(category=category, feature=screen, order=1)
        return category

    @pytest.fixture
    def staff_client(self, api_client):
        """``validate-dto`` is a POST — ReadOnlyOrStaff wants staff for that."""
        from django.contrib.auth import get_user_model

        api_client.force_authenticate(
            get_user_model().objects.create_superuser(
                username="admin", email="admin@test.com", password="pw"
            )
        )
        return api_client

    def _validate(self, api_client, category, features):
        response = api_client.post(
            f"{BASE}/categories/{category.pk}/validate-dto/",
            {"features": features},
            format="json",
        )
        assert response.status_code == 200, response.content
        return response.json()

    def test_a_hidden_mandatory_feature_is_not_required(self, staff_client, category):
        result = self._validate(
            staff_client, category, {"condition": {"type": "select", "value": ["new"]}}
        )
        assert result["valid"] is True, result
        assert all(row["error"] is None for row in result["results"]), result

    def test_the_same_feature_is_required_once_shown(self, staff_client, category):
        result = self._validate(
            staff_client, category, {"condition": {"type": "select", "value": ["used"]}}
        )
        assert result["valid"] is False
        errors = {row["slug"]: row["error"] for row in result["results"] if row["error"]}
        assert errors == {"screen_condition": "mandatory_missing"}


# ── category-wide warnings ───────────────────────────────────────────


class TestFeatureWarnings:
    def test_an_unknown_controlling_slug_is_reported(self):
        category = Category.objects.create(name="Phones", slug="phones")
        feature = _feature(
            rules=[
                {
                    "effect": "require",
                    "when": {"all": [{"feature": "conditoin", "op": "filled"}]},
                }
            ]
        )
        CategoryFeature.objects.create(category=category, feature=feature, order=0)

        warnings = feature_warnings(category)
        assert warnings == ["screen_condition: Rule condition references unknown feature slug: conditoin"]

    def test_a_known_controlling_slug_is_silent(self):
        category = Category.objects.create(name="Phones", slug="phones")
        condition = _feature(name="Condition", slug="condition", config={"type": "bool"})
        target = _feature(rules=CONDITION_RULES)
        CategoryFeature.objects.create(category=category, feature=condition, order=0)
        CategoryFeature.objects.create(category=category, feature=target, order=1)
        assert feature_warnings(category) == []

    def test_an_unknown_parent_feature_is_reported(self, vocabulary_resolver):
        category = Category.objects.create(name="Phones", slug="phones")
        feature = _feature(
            name="Model",
            slug="model",
            config={
                "type": "ref_select",
                "optionsRef": {"vocabulary": VOCABULARY, "level": "Model", "parentFeature": "vendor"},
            },
        )
        CategoryFeature.objects.create(category=category, feature=feature, order=0)
        assert feature_warnings(category) == [
            "model: optionsRef.parentFeature references unknown feature slug: vendor"
        ]

    def test_warnings_never_raise_and_never_block(self):
        """An unknown slug is a review finding, not a validation failure.

        The same feature is reused across categories with different field
        sets, where the controlling slug legitimately reads as ``empty`` —
        so ``Category.clean()`` must still accept the category.
        """
        category = Category.objects.create(name="Phones", slug="phones")
        feature = _feature(
            rules=[
                {"effect": "hide", "when": {"all": [{"feature": "elsewhere", "op": "filled"}]}}
            ]
        )
        CategoryFeature.objects.create(category=category, feature=feature, order=0)
        category.clean()
        assert feature_warnings(category)


# ── feature editor ───────────────────────────────────────────────────


class TestFeatureEditorCarriesTheNewFields:
    @pytest.fixture
    def category(self):
        return Category.objects.create(name="Phones", slug="phones", draft="")

    def _apply(self, category, items):
        revision = Category.objects.values_list("revision", flat=True).get(pk=category.pk)
        apply_feature_editor_changes(category, items, base_revision=revision)

    def _payload(self, **overrides):
        payload = {
            "name": "Screen condition",
            "slug": "screen_condition",
            "config": {"type": "string", "maxLength": 100},
            "rules": CONDITION_RULES,
            "description": "feature.screen_condition.help",
            "example": "No scratches",
            "default": ["intact"],
            "hints": [{"title": "hint.title", "content": "hint.content"}],
            "group": "About the condition",
        }
        payload.update(overrides)
        return payload

    def _assert_carried(self, feature):
        assert feature.rules == CONDITION_RULES
        assert feature.description == "feature.screen_condition.help"
        assert feature.example == "No scratches"
        assert feature.default == ["intact"]
        assert feature.hints == [{"title": "hint.title", "content": "hint.content"}]
        assert feature.group == "About the condition"

    def test_create(self, category):
        self._apply(
            category,
            [FeatureEditorItem(action="create", order=0, feature=self._payload())],
        )
        self._assert_carried(Feature.objects.get(slug="screen_condition", tn_parent__isnull=True))

    def test_edit(self, category):
        feature = _feature()
        CategoryFeature.objects.create(category=category, feature=feature, order=0)
        self._apply(
            category,
            [
                FeatureEditorItem(
                    action="edit", order=0, feature=self._payload(id=feature.pk)
                )
            ],
        )
        feature.refresh_from_db()
        self._assert_carried(feature)

    def test_edit_rejects_a_broken_rule_set(self, category):
        """``edit`` goes through ``Feature.clean()`` — so the grammar is enforced."""
        feature = _feature()
        CategoryFeature.objects.create(category=category, feature=feature, order=0)
        with pytest.raises(ValidationError):
            self._apply(
                category,
                [
                    FeatureEditorItem(
                        action="edit",
                        order=0,
                        feature=self._payload(id=feature.pk, rules=[{"effect": "teleport"}]),
                    )
                ],
            )

    def test_inherit(self, category):
        root = _feature()
        CategoryFeature.objects.create(category=category, feature=root, order=0)
        self._apply(
            category,
            [
                FeatureEditorItem(
                    action="inherit", order=0, feature=self._payload(id=root.pk)
                )
            ],
        )
        override = Feature.objects.get(tn_parent=root)
        self._assert_carried(override)

    def test_the_editor_serializer_defaults_are_empty_not_missing(self):
        """An editor payload that says nothing must not wipe into ``None``."""
        from stapel_categories.serializers import FeatureEditorFeatureSerializer

        serializer = FeatureEditorFeatureSerializer(data={"slug": "colour"})
        assert serializer.is_valid(), serializer.errors
        assert serializer.validated_data["rules"] == []
        assert serializer.validated_data["hints"] == []
        assert serializer.validated_data["description"] == ""
        assert serializer.validated_data["default"] is None


# ── translation keys ─────────────────────────────────────────────────


class TestTranslationKeysCollectFormMetadata:
    def test_metadata_keys_are_collected_alongside_the_name(self, rich_feature):
        keys = collect_feature_translation_keys()
        assert "Screen condition" in keys
        assert "feature.screen_condition.help" in keys
        assert "No scratches" in keys
        assert "About the condition" in keys
        assert "hint.title" in keys
        assert "hint.content" in keys

    def test_translate_none_suppresses_them_like_the_name(self):
        _feature(
            translate=Feature.TranslateMode.NONE,
            description="feature.hidden.help",
            group="Hidden section",
        )
        keys = collect_feature_translation_keys()
        assert "feature.hidden.help" not in keys
        assert "Hidden section" not in keys


# ── admin ────────────────────────────────────────────────────────────


class TestFeatureAdminSmoke:
    """The new fields must be reachable in the admin, not just in the model."""

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

    def test_the_changeform_offers_every_new_field(self, feature_admin, staff_request, rich_feature):
        form_class = feature_admin.get_form(staff_request, rich_feature)
        for field in NEW_FIELDS:
            assert field in form_class.base_fields, f"the Feature changeform has no {field}"

    def test_the_changeform_saves_them(self, feature_admin, staff_request, rich_feature):
        form_class = feature_admin.get_form(staff_request, rich_feature)
        data = {
            "tn_parent": "",
            "translate": "all",
            "slug": rich_feature.slug,
            "name": rich_feature.name,
            "icon": "",
            "comment": "",
            "is_test": False,
            "tn_priority": 0,
            "config": '{"type": "string", "maxLength": 100}',
            "mandatory": False,
            "show_as_badge": False,
            "show_at_title": False,
            "description": "feature.updated.help",
            "example": "e.g. mint",
            "default": '["used"]',
            "hints": '[{"title": "t", "content": "c"}]',
            "group": "Updated section",
            "rules": '[]',
        }
        form = form_class(data, instance=rich_feature)
        assert form.is_valid(), form.errors
        saved = form.save()
        assert saved.description == "feature.updated.help"
        assert saved.default == ["used"]
        assert saved.hints == [{"title": "t", "content": "c"}]
        assert saved.group == "Updated section"

    def test_the_changelist_renders_with_the_new_column(self, feature_admin, staff_request, rich_feature):
        changelist = feature_admin.get_changelist_instance(staff_request)
        assert "group" in feature_admin.get_list_display(staff_request)
        assert rich_feature in changelist.get_queryset(staff_request)

    def test_the_fieldsets_expose_form_and_rules(self, feature_admin, staff_request):
        from django.contrib.admin.utils import flatten_fieldsets

        titles = {title for title, _ in feature_admin.get_fieldsets(staff_request)}
        assert {"Form", "Rules"} <= titles
        exposed = set(flatten_fieldsets(feature_admin.get_fieldsets(staff_request)))
        assert set(NEW_FIELDS) <= exposed

    def test_the_category_changeform_offers_external_id(self, staff_request):
        from stapel_categories.admin import CategoryAdmin

        category_admin = CategoryAdmin(Category, AdminSite())
        form_class = category_admin.get_form(staff_request, None)
        assert "external_id" in form_class.base_fields
