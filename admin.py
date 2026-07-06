"""Django admin for Category and Feature.

The config editor UI is supplied by stapel-attributes (its ``ConfigEditorWidget``
ships the Lit bundle via ``Media``); this module wires the models, the
feature-editor forms, and validation admin-actions that delegate to the
attribute engine. CDN icons are opaque strings — no cdn dependency, no
image preview coupling.
"""
from django.contrib import admin
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from treenode.admin import TreeNodeModelAdmin

from stapel_attributes import get_feature_type, parse_config
from stapel_core.django.admin.mixins import RevisionAdmin

from .forms import CategoryAdminForm, FeatureAdminForm
from .models import Category, CategoryFeature, Feature


class SubFeatureInline(admin.TabularInline):
    model = Feature
    fields = ["name", "slug", "config", "mandatory", "show_as_badge", "show_at_title", "translate", "tn_priority"]
    show_change_link = True
    extra = 1


class FeatureCategoryInline(admin.TabularInline):
    model = CategoryFeature
    fk_name = "feature"
    fields = ["category", "order"]
    show_change_link = True
    extra = 0
    ordering = ["order", "id"]


@admin.register(Category)
class CategoryAdmin(RevisionAdmin, TreeNodeModelAdmin):
    """Category admin with revision tracking + treenode display.

    Inlines are intentionally omitted — use the Feature Editor and Children
    Editor screens (built on stapel-attributes components) instead.
    """

    form = CategoryAdminForm
    inlines = []
    autocomplete_fields = ["tn_parent"]

    list_display = [
        "name_with_status", "tn_priority", "feature_count", "active",
        "carousel_enabled", "catalog_icon", "carousel_icon", "translatable",
        "is_test", "revision", "deleted",
    ]
    list_filter = ["active", "carousel_enabled", "translatable", "is_test", "deleted"]
    search_fields = ["name", "slug"]
    readonly_fields = ["revision"]
    actions = ["undelete_branch", "validate_category_features"]

    fieldsets = (
        ("Basic Information", {
            "fields": ("tn_parent", "translatable", "slug", "name", "comment", "active", "is_test", "tn_priority"),
        }),
        ("Icons", {
            "fields": ("catalog_icon", "carousel_icon", "carousel_enabled"),
            "description": "CDN asset references as opaque strings (e.g. catalog/electronics).",
        }),
        ("Feature Editor", {"fields": ("draft",), "description": "Manage features for this category"}),
        ("Advanced", {"fields": ("revision",), "classes": ("collapse",)}),
    )

    @admin.display(description="ID / Slug", ordering="slug")
    def name_with_status(self, obj):
        id_badge = format_html(
            '<span style="color:#888;font-size:11px;margin-right:4px;">{}</span>', obj.id
        )
        if obj.deleted:
            return format_html(
                "{}"
                '<span style="color:#e74c3c;text-decoration:line-through;opacity:0.6;">{}</span> '
                '<span style="background:#e74c3c;color:#fff;padding:2px 6px;border-radius:3px;'
                'font-size:10px;font-weight:bold;">DELETED</span>',
                id_badge, obj.slug,
            )
        return format_html("{}{}", id_badge, obj.slug)

    @admin.display(description="Features")
    def feature_count(self, obj):
        return obj.features.count()

    @admin.action(description=_("Undelete category branch (category and all descendants)"))
    def undelete_branch(self, request, queryset):
        count = 0
        for category in queryset:
            if category.deleted:
                category.deleted = False
                category.save()
                count += 1
                descendants_pks = category.tn_descendants_pks
                if descendants_pks:
                    descendant_ids = [int(pk) for pk in str(descendants_pks).split(",") if pk]
                    count += Category.objects.filter(
                        id__in=descendant_ids, deleted=True
                    ).update(deleted=False)
        self.message_user(request, _("%d category/categories restored (including descendants).") % count)

    @admin.action(description=_("Validate feature configs in category"))
    def validate_category_features(self, request, queryset):
        errors_by_category = []
        categories_with_errors = 0
        validated_features = {}

        for category in queryset:
            all_features = list(category.get_all_features())
            invalid_results = []
            for feature in all_features:
                if feature.pk in validated_features:
                    result = validated_features[feature.pk]
                else:
                    result = _validate_feature(feature)
                    validated_features[feature.pk] = result
                if result is not None:
                    invalid_results.append(result)

            if invalid_results:
                categories_with_errors += 1
                lines = [f"Category: {category.name} ({category.slug}) [id={category.pk}] "
                         f"— {len(invalid_results)} error(s) of {len(all_features)} feature(s)"]
                for result in invalid_results:
                    lines.append(
                        f"  - feature {result['id']} ({result['slug']}): {result['error']}"
                    )
                errors_by_category.append("\n".join(lines))

        if categories_with_errors > 0:
            message = (
                f"Found errors in {categories_with_errors} of {queryset.count()} category/categories:\n\n"
                + "\n".join(errors_by_category)
            )
            self.message_user(request, message, level="error")
        else:
            self.message_user(request, f"All features in {queryset.count()} category/categories are valid.")


@admin.register(Feature)
class FeatureAdmin(TreeNodeModelAdmin):
    form = FeatureAdminForm
    inlines = [SubFeatureInline, FeatureCategoryInline]
    autocomplete_fields = ["tn_parent"]
    list_display = [
        "name", "slug", "feature_type_display", "config_status", "mandatory",
        "show_as_badge", "show_at_title", "translate", "is_test", "tn_priority",
    ]
    list_filter = ["mandatory", "show_as_badge", "show_at_title", "translate", "is_test"]
    search_fields = ["name", "slug", "comment"]
    actions = ["validate_configs"]

    fieldsets = (
        ("Basic Information", {
            "fields": ("tn_parent", "translate", "slug", "name", "icon", "comment", "is_test", "tn_priority"),
        }),
        ("Type Configuration", {"fields": ("config",)}),
        ("Display Options", {"fields": ("mandatory", "show_as_badge", "show_at_title")}),
    )

    @admin.display(description="Type")
    def feature_type_display(self, obj):
        return obj.feature_type

    @admin.display(description="Config")
    def config_status(self, obj):
        result = _validate_feature(obj)
        if result is None:
            return mark_safe('<span style="color:#27ae60;font-size:16px;" title="Config is valid">&#10003;</span>')
        return format_html(
            '<span style="color:#e74c3c;font-size:16px;" title="{}">&#10007;</span>', result["error"]
        )

    @admin.action(description=_("Validate feature configs"))
    def validate_configs(self, request, queryset):
        errors = []
        valid_count = 0
        for feature in queryset:
            result = _validate_feature(feature)
            if result is None:
                valid_count += 1
            else:
                errors.append(
                    f"id={feature.pk} name={feature.name} slug={feature.slug}: {result['error']}"
                )
        if errors:
            message = (
                f"Validation failed for {len(errors)} of {queryset.count()} feature(s):\n\n"
                + "\n".join(errors)
            )
            self.message_user(request, message, level="error")
        else:
            self.message_user(request, f"All {valid_count} feature(s) are valid.")


def _validate_feature(feature):
    """Validate one feature's config via stapel-attributes. Returns None if valid."""
    try:
        config = parse_config(feature.get_config_with_defaults())
        feature_type = get_feature_type(config.type)
        feature_type.validate_config(config)
        return None
    except (DjangoValidationError, ValueError) as exc:
        error_msg = str(exc.messages[0]) if hasattr(exc, "messages") else str(exc)
        return {"id": feature.pk, "slug": feature.slug, "name": feature.name, "error": error_msg}
