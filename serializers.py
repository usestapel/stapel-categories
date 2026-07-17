"""Category and Feature serializers.

Feature ``config`` is polymorphic; the OpenAPI schema and the proxy
serializer come from stapel-attributes (``get_feature_config_proxy_serializer``)
— this module does not describe attribute types itself.
"""
from drf_spectacular.extensions import OpenApiSerializerFieldExtension
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from stapel_attributes import get_feature_config_proxy_serializer
from stapel_core.django.api.serializers import StapelDataclassSerializer

from .dto import FeatureEditorDraftResponse, UndeleteResponse
from .models import Category, Feature


class FeatureConfigSchemaField(serializers.JSONField):
    """JSONField with polymorphic OpenAPI schema."""

    pass


class FeatureConfigFieldExtension(OpenApiSerializerFieldExtension):
    """OpenAPI extension mapping FeatureConfigSchemaField to the FeatureConfig schema."""

    target_class = FeatureConfigSchemaField

    def map_serializer_field(self, auto_schema, direction):
        return {"$ref": "#/components/schemas/FeatureConfig"}


# =============================================================================
# Main Feature Serializers
# =============================================================================


class FeatureSerializer(serializers.ModelSerializer):
    """Feature serializer with polymorphic config support."""

    config = serializers.SerializerMethodField()

    @extend_schema_field(get_feature_config_proxy_serializer())
    def get_config(self, obj):
        return obj.config

    class Meta:
        model = Feature
        fields = [
            "id", "name", "slug", "icon", "comment",
            "config",
            "mandatory", "show_as_badge", "show_at_title", "translate",
            "tn_parent", "tn_priority",
            "tn_ancestors_pks", "tn_children_pks",
            "tn_descendants_pks", "tn_siblings_pks",
        ]


class FeatureCompactSerializer(serializers.ModelSerializer):
    """Compact feature serializer for list endpoints and embedded feature data."""

    config = serializers.SerializerMethodField()

    @extend_schema_field(get_feature_config_proxy_serializer())
    def get_config(self, obj):
        return obj.config

    class Meta:
        model = Feature
        fields = [
            "id", "tn_parent", "name", "slug", "icon", "comment",
            "config",
            "mandatory", "show_as_badge", "show_at_title", "translate",
        ]


class FeatureBulkSerializer(serializers.ModelSerializer):
    """Serializer for bulk add/update operations — id is required."""

    id = serializers.IntegerField(required=True)
    config = serializers.JSONField(required=False)

    class Meta:
        model = Feature
        fields = "__all__"


class FeatureCreateUpdateSerializer(serializers.ModelSerializer):
    """Serializer for creating/updating features with polymorphic config."""

    config = FeatureConfigSchemaField(required=False, default=dict)

    class Meta:
        model = Feature
        fields = "__all__"


# =============================================================================
# Category Serializers
# =============================================================================


class CategorySerializer(serializers.ModelSerializer):
    """Category serializer with feature references and revision tracking."""

    class Meta:
        model = Category
        fields = [
            "id", "name", "slug", "catalog_icon", "carousel_icon", "carousel_enabled", "active",
            "features", "translatable",
            "tn_parent", "tn_priority",
            "tn_ancestors_pks", "tn_children_pks",
            "revision", "deleted",
        ]
        read_only_fields = ["revision"]


class CategoryWithFeaturesSerializer(serializers.ModelSerializer):
    """Category serializer with expanded feature details."""

    features = FeatureCompactSerializer(many=True, read_only=True)
    feature_schema = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = [
            "id", "name", "catalog_icon", "carousel_icon", "carousel_enabled", "active",
            "features", "feature_schema",
            "tn_parent", "tn_priority",
            "tn_ancestors_pks", "tn_children_pks",
        ]

    def get_feature_schema(self, obj):
        return obj.get_feature_schema()


class CategoryBulkSerializer(serializers.ModelSerializer):
    """Serializer for bulk add/update operations — id is required."""

    id = serializers.IntegerField(required=True)

    class Meta:
        model = Category
        fields = ["id", "name", "slug", "catalog_icon", "carousel_icon", "features", "tn_parent", "tn_priority"]


# =============================================================================
# Feature editor serializers
# =============================================================================

FEATURE_EDITOR_ACTIONS = ["keep", "add", "edit", "inherit", "remove", "create", "replace"]


class FeatureEditorFeatureSerializer(serializers.Serializer):
    """Writable feature payload for the category feature editor.

    Uses Serializer instead of ModelSerializer to avoid model-level
    validation (like the unique slug constraint) since this is just a data
    container for the editor.
    """

    id = serializers.IntegerField(required=False, allow_null=True)
    name = serializers.CharField(required=False, allow_blank=True, default="")
    slug = serializers.CharField(required=False, allow_blank=True, default="")
    icon = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    comment = serializers.CharField(required=False, allow_blank=True, default="")
    config = FeatureConfigSchemaField(required=False, default=dict)
    mandatory = serializers.BooleanField(required=False, default=False)
    show_as_badge = serializers.BooleanField(required=False, default=False)
    show_at_title = serializers.BooleanField(required=False, default=False)
    translate = serializers.ChoiceField(
        choices=["all", "title", "none"],
        required=False,
        default="all",
        help_text="What to translate: 'all' = title + options, 'title' = title only, 'none' = nothing",
    )
    tn_parent = serializers.IntegerField(required=False, allow_null=True)
    tn_priority = serializers.IntegerField(required=False, default=0)


class FeatureEditorItemSerializer(serializers.Serializer):
    """Item from the feature editor list."""

    order = serializers.IntegerField()
    action = serializers.ChoiceField(choices=FEATURE_EDITOR_ACTIONS)
    feature = FeatureEditorFeatureSerializer()
    replace_with = serializers.IntegerField(required=False, allow_null=True)

    def validate(self, attrs):
        action = attrs.get("action")
        feature_data = attrs.get("feature") or {}
        feature_id = feature_data.get("id")
        slug = feature_data.get("slug")

        if action in ("keep", "edit", "remove") and not feature_id:
            raise serializers.ValidationError("feature.id is required for keep/edit/remove actions")  # noqa: R002

        if action == "add" and not feature_id:
            raise serializers.ValidationError("feature.id (root feature id) is required for add")  # noqa: R002

        if action == "inherit":
            if not feature_id:
                raise serializers.ValidationError("feature.id is required for inherit")  # noqa: R002
            if not slug:
                raise serializers.ValidationError("slug is required for inherit")  # noqa: R002

        if action == "create":
            if not slug:
                raise serializers.ValidationError("slug is required for create")  # noqa: R002

        if action == "replace":
            replace_with = attrs.get("replace_with")
            if not replace_with:
                raise serializers.ValidationError("replace_with is required for replace action")  # noqa: R002
            if not feature_id:
                raise serializers.ValidationError("feature.id is required for replace action")  # noqa: R002

        return attrs


class FeatureEditorApplySerializer(serializers.Serializer):
    """Request payload for applying feature editor changes."""

    features = FeatureEditorItemSerializer(many=True)
    draft = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    # Optimistic-concurrency token (M-5): the revision the editor was loaded
    # against (echoed from the feature-editor state's ``revision``). Must
    # still match, else the apply is rejected with 409.
    base_revision = serializers.IntegerField()


# =============================================================================
# Feature Editor Response Serializers
# =============================================================================


class FeatureEditorDraftSerializer(serializers.Serializer):
    """Draft serializer for feature editor."""

    draft = serializers.CharField(required=False, allow_blank=True, default="")


class FeatureEditorStateItemSerializer(serializers.Serializer):
    """Single item in feature editor state."""

    order = serializers.IntegerField()
    available_actions = serializers.ListField(
        child=serializers.ChoiceField(choices=FEATURE_EDITOR_ACTIONS)
    )
    action = serializers.ChoiceField(choices=FEATURE_EDITOR_ACTIONS)
    feature = FeatureSerializer()
    parent_feature = FeatureSerializer(required=False, allow_null=True)


class FeatureEditorStateSerializer(serializers.Serializer):
    """Response serializer for feature editor state."""

    features = FeatureEditorStateItemSerializer(many=True)
    available_root_features = FeatureSerializer(many=True)
    draft = serializers.CharField(required=False, allow_blank=True, default="")
    revision = serializers.IntegerField(required=False)


# =============================================================================
# Category Command Pattern Serializers
# =============================================================================

CATEGORY_COMMANDS = ["keep", "add", "edit", "delete", "reorder"]


class CategoryCommandSerializer(serializers.Serializer):
    """Serializer for category command pattern."""

    id = serializers.IntegerField(required=False, allow_null=True, help_text="Category ID (null for add command)")
    command = serializers.ChoiceField(choices=CATEGORY_COMMANDS, help_text="Command to execute")
    name = serializers.CharField(required=False, allow_blank=True, help_text="Category name (for add/edit)")
    slug = serializers.CharField(required=False, allow_blank=True, help_text="Category slug (for add/edit)")
    translatable = serializers.BooleanField(required=False, default=True, help_text="If True, name is translation key")
    parent_id = serializers.IntegerField(required=False, allow_null=True, help_text="Parent category ID (for add)")
    priority = serializers.IntegerField(required=False, help_text="Tree node priority (for add/reorder)")

    def validate(self, attrs):
        command = attrs.get("command")
        category_id = attrs.get("id")

        if command == "add":
            if not attrs.get("name"):
                raise serializers.ValidationError({"name": "Name is required for add command"})  # noqa: R002
            if not attrs.get("slug"):
                raise serializers.ValidationError({"slug": "Slug is required for add command"})  # noqa: R002

        elif command in ("edit", "delete", "keep", "reorder"):
            if not category_id:
                raise serializers.ValidationError({"id": f"Category ID is required for {command} command"})  # noqa: R002

        return attrs


class CategoryBulkCommandSerializer(serializers.Serializer):
    """Serializer for bulk category commands."""

    categories = CategoryCommandSerializer(many=True)


# =============================================================================
# Validation Request Serializers
# =============================================================================


class ValidateDtoRequestSerializer(serializers.Serializer):
    """Request serializer for validate_dto endpoint."""

    features = serializers.JSONField(
        help_text="Features DTO object keyed by feature slug: {slug: {type, value, ...}}"
    )


# =============================================================================
# Dataclass Serializers
# =============================================================================


class UndeleteResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = UndeleteResponse


class FeatureEditorDraftResponseSerializer(StapelDataclassSerializer):
    class Meta:
        dataclass = FeatureEditorDraftResponse
