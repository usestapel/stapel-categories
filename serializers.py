"""Category and Feature serializers.

Feature ``config`` is polymorphic; the OpenAPI schema and the proxy
serializer come from stapel-attributes (``get_feature_config_proxy_serializer``)
— this module does not describe attribute types itself.
"""
from drf_spectacular.extensions import OpenApiSerializerFieldExtension
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from stapel_attributes import (
    get_feature_config_proxy_serializer,
    get_feature_dto_proxy_serializer,
    get_feature_dto_serializer_class,
)
from stapel_core.django.api.serializers import StapelDataclassSerializer

from .dto import FeatureEditorDraftResponse, UndeleteResponse
from .models import CHILDREN_AS_AUTHORED_CHOICES, Category, Feature


class FeatureConfigSchemaField(serializers.JSONField):
    """JSONField with polymorphic OpenAPI schema."""

    pass


class FeatureConfigFieldExtension(OpenApiSerializerFieldExtension):
    """OpenAPI extension mapping FeatureConfigSchemaField to the FeatureConfig schema."""

    target_class = FeatureConfigSchemaField

    def map_serializer_field(self, auto_schema, direction):
        return {"$ref": "#/components/schemas/FeatureConfig"}


class FeaturesDtoField(serializers.DictField):
    """``{slug: FeatureDto}`` — a values-DTO object keyed by feature slug.

    Same shape stapel-listings' ``ListingFeaturesInputField`` types for the
    publish path; ``validate-dto`` takes the identical envelope (A1 delta:
    typed instead of the bare ``JSONField`` this endpoint shipped with).
    """

    def __init__(self, **kwargs):
        super().__init__(child=get_feature_dto_serializer_class()(), **kwargs)


class FeaturesDtoFieldExtension(OpenApiSerializerFieldExtension):
    target_class = FeaturesDtoField

    def map_serializer_field(self, auto_schema, direction):
        dto_proxy = get_feature_dto_proxy_serializer()
        auto_schema.resolve_serializer(dto_proxy, direction)
        return {
            "type": "object",
            "additionalProperties": {"$ref": "#/components/schemas/FeatureDto"},
        }


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
            "mandatory", "show_as_badge", "show_at_title", "visibility", "translate",
            "rules", "description", "example", "default", "hints", "group",
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
            "mandatory", "show_as_badge", "show_at_title", "visibility", "translate",
            "rules", "description", "example", "default", "hints", "group",
        ]


class FeatureBulkSerializer(serializers.ModelSerializer):
    """Serializer for bulk add/update operations — id is required."""

    id = serializers.IntegerField(required=True)
    config = FeatureConfigSchemaField(required=False, default=dict)

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
    """The PUBLIC category projection — every anonymous read serves this.

    List, detail, ``children``, ``roots``, ``carousel`` and ``by-slug`` all
    answer to strangers (the whole surface is ``ReadOnlyOrStaff``), so which
    keys ride here is a disclosure decision. ``external_id`` /
    ``external_source`` do NOT: they are the source catalogue's own node ids,
    stamped by ``load_catalog`` so a re-import can find its rows again — an
    operator fact. A stand that imported a competitor's catalogue was serving
    that catalogue's internal numbering to anyone with curl, one key per row.
    Provenance lives on the staff surfaces only: the Django admin, the
    staff-gated bulk serializer, and :class:`CategoryStaffSerializer` on the
    write actions.

    The exact key set is frozen by ``test_public_read.
    PUBLIC_CATEGORY_KEYS`` — adding a field here is a conscious act that
    extends that contract in the same commit.
    """

    children_as = serializers.SerializerMethodField(
        help_text=(
            "How this category's children are presented: `tiles` (real "
            "subcategories) or `chips` (a partition of one attribute "
            "template). `null` when the category has no children. The "
            "authoring value `auto` is resolved server-side and never "
            "appears here."
        )
    )

    class Meta:
        model = Category
        fields = [
            "id", "name", "slug",
            "catalog_icon", "carousel_icon", "carousel_enabled", "active",
            "children_as",
            "features", "translatable",
            "tn_parent", "tn_priority",
            "tn_ancestors_pks", "tn_children_pks",
            "revision", "deleted",
        ]
        read_only_fields = ["revision"]

    @extend_schema_field(
        serializers.ChoiceField(choices=["tiles", "chips"], allow_null=True)
    )
    def get_children_as(self, obj) -> str | None:
        # Three columns already on the row (see Category.resolved_children_as)
        # — no query, so a page of N categories costs exactly what it did
        # before this key existed.
        return obj.resolved_children_as


class CategoryStaffSerializer(CategorySerializer):
    """The public projection plus provenance — staff writes only.

    ``create``/``update``/``partial_update`` are gated by ``ReadOnlyOrStaff``,
    and an operator hand-fixing an imported row legitimately needs to see and
    set ``external_id``/``external_source`` (re-pointing a row at its source
    node is how a botched match is repaired). Extending the public class keeps
    the two projections structurally one serializer: a field added to the
    public set is automatically part of this one, and this one can never lose
    a public field silently.

    ``children_as_authored`` is the same split, one field further: the
    inherited ``children_as`` stays the RESOLVED read (an operator wants to
    see what a visitor sees), and the raw authoring column — ``auto``
    included — gets its own key, writable. Without it "an authored value wins
    over derivation" would be reachable only from the admin or a DB console.
    ``children_as_derived`` rides along read-only so an operator can tell an
    inherited decision from their own.
    """

    children_as_authored = serializers.ChoiceField(
        source="children_as",
        choices=CHILDREN_AS_AUTHORED_CHOICES,
        required=False,
        help_text=(
            "Authoring value: `auto` leaves it to `derive_children_as`, "
            "`tiles`/`chips` pin it."
        ),
    )
    children_as_derived = serializers.CharField(read_only=True)

    class Meta(CategorySerializer.Meta):
        fields = CategorySerializer.Meta.fields + [
            "external_id", "external_source",
            "children_as_authored", "children_as_derived",
        ]


class CategoryTreeNodeSerializer(serializers.Serializer):
    """One node of ``GET /categories/api/v1/tree/`` — schema, not machinery.

    The endpoint assembles plain dicts from a single flat ``values()`` read
    (a nested serializer over model instances would be one query per level),
    so this class exists to give the emitted contract a named shape rather
    than to run.

    ``children`` is declared as a list of objects instead of recursing into
    this same serializer: the nesting is depth-capped at request time, and a
    self-referential component makes a generator that inlines definitions
    recur until it gives up. The element shape is this one.

    Provenance (``external_id``/``external_source``) is absent here for the
    reason :class:`CategorySerializer` states — this is an anonymous read.
    """

    id = serializers.IntegerField()
    slug = serializers.CharField()
    name = serializers.CharField()
    path = serializers.CharField(
        help_text=(
            "Ancestor ids root->self, `/`-joined (e.g. `141/151/166`) — the "
            "form the search query's `category` parameter takes."
        )
    )
    catalog_icon = serializers.CharField(allow_blank=True)
    children_as = serializers.ChoiceField(
        choices=["tiles", "chips"],
        allow_null=True,
        help_text="`null` when the node has no children.",
    )
    children = serializers.ListField(
        child=serializers.DictField(),
        help_text="Nodes of this same shape; empty at the requested depth.",
    )


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
        fields = [
            "id", "name", "slug", "external_id", "external_source",
            "catalog_icon", "carousel_icon",
            "features", "tn_parent", "tn_priority",
        ]


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
    visibility = serializers.ChoiceField(
        choices=["public", "owner", "staff"],
        required=False,
        default="public",
        help_text=(
            "Who may READ a stored value: 'public' = anyone (the default), "
            "'owner' = the object's owner and staff, 'staff' = staff only. The "
            "value is still required, validated and stored either way; a "
            "non-public feature is simply never a title and never a badge."
        ),
    )
    translate = serializers.ChoiceField(
        choices=["all", "title", "none"],
        required=False,
        default="all",
        help_text="What to translate: 'all' = title + options, 'title' = title only, 'none' = nothing",
    )
    rules = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        default=list,
        help_text="Conditional rules over sibling values (stapel-attributes' closed grammar).",
    )
    description = serializers.CharField(required=False, allow_blank=True, default="")
    example = serializers.CharField(required=False, allow_blank=True, default="")
    default = serializers.JSONField(required=False, allow_null=True, default=None)
    hints = serializers.ListField(
        child=serializers.DictField(child=serializers.CharField()),
        required=False,
        default=list,
        help_text="Notices rendered with the field: [{title, content}].",
    )
    group = serializers.CharField(required=False, allow_blank=True, default="")
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

    features = FeaturesDtoField(
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
