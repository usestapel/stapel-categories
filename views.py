"""DRF views for stapel-categories.

Ported from the legacy catalog's ``categories/views.py``. Value/config validation
delegates to stapel-attributes' structured pipeline
(``validate_dto_structured`` / ``validate_configs_structured``), fed the
category's resolved ``feature_defs()`` — this module never re-implements the
attribute engine. Permissions (staff-only writes, service-only translation
keys, read-only public) mirror the source via stapel-core permissions.
"""
from django.core.cache import cache
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from rest_framework import serializers as drf_serializers
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from stapel_attributes import validate_configs_structured, validate_dto_structured
from stapel_attributes.results import ValidationBatchResultSerializer
from stapel_core.django.api.errors import StapelErrorResponse
from stapel_core.django.api.permissions import (
    IsServiceRequest,
    IsStaffUser,
    ReadOnlyOrStaff,
)
from stapel_core.django.api.revision import (
    REVISION_PARAMETERS,
    RevisionPagination,
    RevisionViewSetMixin,
)
from stapel_core.django.jwt.utils import reset_sequences_for_models
from stapel_core.django.openapi.schemas import BulkUpdateResponseSerializer

from .conf import categories_settings
from .dto import FeatureEditorDraftResponse, UndeleteResponse
from .errors import (
    ERR_400_CATEGORY_NOT_DELETED,
    ERR_400_CONFIG_REQUIRED,
    ERR_400_DATABASE_ERROR,
    ERR_400_DUPLICATE_SLUG,
    ERR_400_EXPECTED_LIST,
    ERR_400_FEATURE_EDITOR_INVALID,
    ERR_400_INVALID_CONVERSION,
    ERR_404_SLUG_NOT_FOUND,
    ERR_409_FEATURE_EDITOR_CONFLICT,
)
from .feature_editor import (
    FeatureEditorConflict,
    FeatureEditorError,
    FeatureEditorItem,
    apply_feature_editor_changes,
    build_editor_state,
)
from .models import Category, Feature
from .serializers import (
    CategoryBulkCommandSerializer,
    CategoryBulkSerializer,
    CategorySerializer,
    CategoryStaffSerializer,
    FeatureBulkSerializer,
    FeatureCompactSerializer,
    FeatureConfigSchemaField,
    FeatureCreateUpdateSerializer,
    FeatureEditorApplySerializer,
    FeatureEditorDraftResponseSerializer,
    FeatureEditorDraftSerializer,
    FeatureEditorStateSerializer,
    FeatureSerializer,
    UndeleteResponseSerializer,
    ValidateDtoRequestSerializer,
)


#: Cache key prefix of the roots response, completed by a fingerprint of the
#: tree's revision state — the same mechanism the suggest index uses
#: (``functions.py``). A TTL alone would be the ``categories_carousel``
#: bargain: an edit is invisible until the clock runs out. Here the key
#: itself changes when the tree does, so a mutation retires the entry
#: immediately and the timeout is only the ceiling on how long an UNCHANGED
#: tree keeps one.
_ROOTS_CACHE_PREFIX = "stapel_categories:roots:"


def _roots_cache_key() -> str:
    """One cheap aggregate; ``(max revision, row count)`` names the tree.

    Both halves are needed: ``revision`` alone misses a pure deletion, and
    the row count alone misses an edit that keeps the count the same.
    """
    from django.db.models import Count, Max

    fingerprint = Category.objects.aggregate(
        revision=Max("revision"), rows=Count("pk")
    )
    return (
        f"{_ROOTS_CACHE_PREFIX}"
        f"{fingerprint['revision'] or 0}:{fingerprint['rows'] or 0}"
    )


def visible_categories():
    """The rows the public tree reads are allowed to return.

    ONE definition, called by ``children``, ``roots`` and ``by-slug``, so the
    three cannot drift into showing different catalogues. That drift is not
    hypothetical: two of these are new, and the obvious way to write them is
    to copy the filter out of ``children`` — which works right up until
    somebody changes one of the copies.

    What it filters, and what it deliberately does not:

    * ``deleted=False`` — a soft-deleted category is gone as far as any
      reader is concerned. ``deleted-children`` is the staff view that asks
      for them on purpose.
    * ``active`` is **not** filtered, and that is the pre-existing contract
      of ``children``. An inactive category still occupies a place in the
      tree; hiding it here would open a hole under the live categories
      beneath it, and the serializer ships ``active`` on every row so a
      client that wants to grey one out can.
    * ``is_test`` is **not** filtered either, for the reason the model states
      at its declaration: it is an *export* filter (``export_catalog``
      excludes it from committed fixtures), not a runtime-visibility gate. A
      deployment that wants test rows hidden from the storefront hides them
      with ``active``, which is what that field is for.

    Changing any of the three is a change to all three reads at once, which
    is the point of it being one function.
    """
    return Category.objects.filter(deleted=False)


@extend_schema(tags=["Categories"])
class CategoryViewSet(RevisionViewSetMixin, viewsets.ModelViewSet):
    """ViewSet for Category with revision-based synchronization.

    **Sync flow:**
    1. Initial sync: ``GET /categories/`` — returns all categories with revision info
    2. Store ``revisions.global_max`` from the response
    3. Subsequent sync: ``GET /categories/?min_revision={stored_max}`` — only changes
    4. Handle items with ``deleted=true`` by removing them locally
    """

    serializer_class = CategorySerializer
    queryset = Category.objects.all()
    permission_classes = [ReadOnlyOrStaff]
    pagination_class = RevisionPagination

    def get_serializer_class(self):
        # Two projections, one disclosure rule: the write actions are the
        # staff-gated part of this viewset (ReadOnlyOrStaff refuses anonymous
        # writes before a serializer is ever built), so they carry provenance
        # (external_id/external_source); every read action serves the public
        # projection, which does not. See CategorySerializer's docstring.
        if self.action in ("create", "update", "partial_update"):
            return CategoryStaffSerializer
        return CategorySerializer

    @extend_schema(
        description="List categories with revision-based pagination.",
        parameters=REVISION_PARAMETERS,
        responses={200: CategorySerializer},
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        description="Get categories for carousel display (active and carousel_enabled).",
        responses={200: CategorySerializer(many=True)},
    )
    @action(detail=False, methods=["get"], pagination_class=None)
    def carousel(self, request):  # noqa: R007
        """Return active categories with carousel_enabled=True, cached."""
        cache_key = "categories_carousel"
        cached_data = cache.get(cache_key)

        if cached_data is None:
            queryset = Category.objects.filter(
                active=True, carousel_enabled=True, deleted=False
            ).order_by("-tn_priority")
            cached_data = CategorySerializer(queryset, many=True).data
            cache.set(cache_key, cached_data, timeout=categories_settings.CAROUSEL_CACHE_TIMEOUT)

        response = Response(cached_data)
        response["Cache-Control"] = f"public, max-age={categories_settings.CAROUSEL_CACHE_TIMEOUT}"
        return response

    @extend_schema(
        description="Bulk create or update categories. Provide an array of category objects with IDs.",
        request=CategoryBulkSerializer(many=True),
        responses={200: BulkUpdateResponseSerializer, 400: OpenApiTypes.OBJECT},
        examples=[
            OpenApiExample(
                "Bulk add categories",
                value=[
                    {"id": 1, "name": "Electronics", "tn_priority": 10},
                    {"id": 2, "name": "Vehicles", "tn_parent": 1},
                ],
                request_only=True,
            ),
        ],
    )
    @action(detail=False, methods=["post"], permission_classes=[IsStaffUser])
    def bulk_add(self, request):  # noqa: R007
        data = request.data
        if not isinstance(data, list):
            return StapelErrorResponse(400, ERR_400_EXPECTED_LIST)

        updated = []
        for item in data:
            item_id = item.get("id")
            if not item_id:
                continue

            parent_id = item.get("tn_parent")
            parent = None
            if parent_id:
                try:
                    parent = Category.objects.get(pk=parent_id)
                except Category.DoesNotExist:
                    parent = None

            defaults = {
                "name": item.get("name", ""),
                "slug": item.get("slug", ""),
                "tn_parent": parent,
                "tn_priority": item.get("tn_priority", 0),
                "catalog_icon": item.get("catalog_icon") or "",
                "carousel_icon": item.get("carousel_icon") or "",
                "carousel_enabled": item.get("carousel_enabled", False),
                "active": item.get("active", True),
                "translatable": item.get("translatable", True),
            }

            obj, _created = Category.objects.update_or_create(id=item_id, defaults=defaults)
            updated.append(obj.pk)

        reset_sequences_for_models(Category)
        return Response({"updated_ids": updated}, status=status.HTTP_200_OK)  # noqa: R001

    @extend_schema(
        description="Get all features for this category, sorted by order. Includes inherited features.",
        responses={200: FeatureCompactSerializer(many=True)},
        parameters=[],
    )
    @action(detail=True, methods=["get"], url_path="features", pagination_class=None)
    def category_features(self, request, pk=None):  # noqa: R007
        """Return full feature objects for this category, sorted by order."""
        category = self.get_object()
        features = category.get_all_features()
        return Response(FeatureCompactSerializer(features, many=True).data)  # noqa: R001

    @extend_schema(
        tags=["Feature Editor"],
        description="Get feature editor state for admin UI.",
        responses={200: FeatureEditorStateSerializer},
    )
    @action(detail=True, methods=["get"], url_path="feature-editor", permission_classes=[IsStaffUser])
    def feature_editor(self, request, pk=None):  # noqa: R007
        category = self.get_object()
        return Response(build_editor_state(category))  # noqa: R001

    @extend_schema(
        tags=["Feature Editor"],
        description="Save feature editor draft without applying changes.",
        request=FeatureEditorDraftSerializer,
        responses={200: FeatureEditorDraftResponseSerializer},
    )
    @action(detail=True, methods=["post"], url_path="feature-editor/draft", permission_classes=[IsStaffUser])
    def feature_editor_draft(self, request, pk=None):  # noqa: R007
        category = self.get_object()
        new_draft = request.data.get("draft") or ""
        # Draft is editor scratch state, not part of the resolved schema. Persist
        # only the column via a QuerySet.update — this bypasses RevisionMixin.save
        # and its post_save fanout, so an autosave neither bumps the category
        # revision nor emits category.changed (L-8; also sidesteps the
        # phantom-revision H-3 that save(update_fields=["draft"]) would cause).
        Category.objects.filter(pk=category.pk).update(draft=new_draft)
        dto = FeatureEditorDraftResponse(draft=new_draft)
        return Response(FeatureEditorDraftResponseSerializer(dto).data)  # noqa: R001

    @extend_schema(
        tags=["Feature Editor"],
        description="Apply feature editor actions to category and descendants.",
        request=FeatureEditorApplySerializer,
        responses={200: FeatureEditorStateSerializer},
    )
    @action(detail=True, methods=["post"], url_path="feature-editor/apply", permission_classes=[IsStaffUser])
    def feature_editor_apply(self, request, pk=None):  # noqa: R007
        from django.core.exceptions import ValidationError as DjangoValidationError
        from django.db import IntegrityError

        category = self.get_object()
        serializer = FeatureEditorApplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        payload = serializer.validated_data
        items = [
            FeatureEditorItem(
                action=item["action"],
                order=item["order"],
                feature=item["feature"],
                replace_with=item.get("replace_with"),
            )
            for item in payload.get("features", [])
        ]

        try:
            apply_feature_editor_changes(
                category, items, base_revision=payload["base_revision"]
            )
        except FeatureEditorConflict as e:
            return StapelErrorResponse(
                409,
                ERR_409_FEATURE_EDITOR_CONFLICT,
                params={"expected": e.expected, "actual": e.actual},
            )
        except FeatureEditorError as e:
            return StapelErrorResponse(
                400, ERR_400_FEATURE_EDITOR_INVALID, params={"reason": str(e)}
            )
        except DjangoValidationError as e:
            return StapelErrorResponse(
                400,
                ERR_400_FEATURE_EDITOR_INVALID,
                params={"reason": "; ".join(e.messages)},
            )
        except IntegrityError as e:
            error_msg = str(e)
            if "duplicate key" in error_msg and "slug" in error_msg:
                import re

                match = re.search(r"Key \(slug\)=\(([^)]+)\)", error_msg)
                slug = match.group(1) if match else "unknown"
                return StapelErrorResponse(400, ERR_400_DUPLICATE_SLUG, params={"slug": slug})
            return StapelErrorResponse(400, ERR_400_DATABASE_ERROR)

        # Clear the draft without bumping revision or emitting category.changed
        # (H-3/L-8): a QuerySet.update writes just the column, bypassing
        # RevisionMixin.save and its post_save fanout — the apply above already
        # emitted the real schema-change events.
        Category.objects.filter(pk=category.pk).update(draft="")
        category.refresh_from_db()
        return Response(build_editor_state(category))  # noqa: R001

    @extend_schema(
        description="Validate a features DTO against this category's schema.",
        request=ValidateDtoRequestSerializer,
        responses={200: ValidationBatchResultSerializer},
    )
    @action(detail=True, methods=["post"], url_path="validate-dto")
    def validate_dto(self, request, pk=None):  # noqa: R007
        """Validate features DTO against category (delegates to stapel-attributes)."""
        category = self.get_object()
        features_dto = request.data.get("features", {})
        result = validate_dto_structured(category.feature_defs(), features_dto)
        return Response(ValidationBatchResultSerializer(result).data)  # noqa: R001

    @extend_schema(
        description="Validate all feature configs in this category.",
        responses={200: ValidationBatchResultSerializer},
    )
    @action(detail=True, methods=["get"], url_path="validate-configs")
    def validate_configs(self, request, pk=None):  # noqa: R007
        """Validate all feature configs in category (delegates to stapel-attributes)."""
        category = self.get_object()
        result = validate_configs_structured(category.feature_defs())
        return Response(ValidationBatchResultSerializer(result).data)  # noqa: R001

    @extend_schema(
        description="Get all non-deleted children of this category, sorted by tn_priority descending.",
        responses={200: CategorySerializer(many=True)},
        parameters=[],
    )
    @action(detail=True, methods=["get"], url_path="children", pagination_class=None)
    def children(self, request, pk=None):  # noqa: R007
        """Return non-deleted children, sorted by tn_priority descending."""
        category = self.get_object()
        children = visible_categories().filter(tn_parent=category).order_by(
            "-tn_priority", "id"
        )
        response = Response(CategorySerializer(children, many=True).data)  # noqa: R001
        response["Cache-Control"] = (
            f"public, max-age={categories_settings.TREE_CACHE_TIMEOUT}"
        )
        return response

    @extend_schema(
        description=(
            "Top-level categories (no parent), sorted by tn_priority "
            "descending. The entry point of the tree, without the whole table."
        ),
        responses={200: CategorySerializer(many=True)},
        parameters=[],
    )
    @action(detail=False, methods=["get"], url_path="roots", pagination_class=None)
    def roots(self, request):  # noqa: R007
        """The tree's first rung.

        Until this existed a client that wanted "what are the top-level
        categories?" had exactly one way to ask: list the whole table and
        filter it client-side. On a real catalogue that is hundreds of
        kilobytes of JSON to render a row of tiles — the storefront's cold
        ``/c`` measured **21 seconds** — and the server had no way to answer
        the question that was actually being asked.

        ``children`` is the same read one level down and has always existed,
        which is what made the gap easy to miss: the tree was walkable from
        the second rung on, and only the first had no door.

        Same visibility rule as ``children`` by construction
        (``visible_categories``), unpaginated (a catalogue's roots are tens
        of rows, not thousands), and cached at the edge for
        ``TREE_CACHE_TIMEOUT``.
        """
        cache_key = _roots_cache_key()
        cached_data = cache.get(cache_key)
        if cached_data is None:
            queryset = visible_categories().filter(tn_parent__isnull=True).order_by(
                "-tn_priority", "id"
            )
            cached_data = CategorySerializer(queryset, many=True).data
            cache.set(
                cache_key, cached_data, timeout=categories_settings.TREE_CACHE_TIMEOUT
            )

        response = Response(cached_data)
        response["Cache-Control"] = (
            f"public, max-age={categories_settings.TREE_CACHE_TIMEOUT}"
        )
        return response

    @extend_schema(
        description=(
            "Retrieve one category by its slug. `slug` is unique, so this is "
            "an alternate primary key, not a search."
        ),
        responses={200: CategorySerializer, 404: OpenApiTypes.OBJECT},
        parameters=[],
    )
    @action(
        detail=False,
        methods=["get"],
        url_path=r"by-slug/(?P<slug>[^/.]+)",
        pagination_class=None,
    )
    def by_slug(self, request, slug=None):  # noqa: R007
        """Resolve a slug to a category.

        The storefront's URLs are slugs (``/c/electronics``), and the server
        only ever accepted numeric ids — so every category page began by
        pulling the entire table to find one row. That is the other half of
        the 21-second cold ``/c``.

        A path segment and not a ``?slug=`` filter, deliberately: ``slug`` is
        ``unique=True``, so this resolves an alternate primary key and
        returns an object, not a list of at most one. A query parameter would
        have made the caller unwrap a collection to express "get this
        category", and would have implied a filter contract (``?slug=a&
        slug=b``, partial matches) that a unique key does not have.

        Honours the same visibility rule as ``children`` and ``roots``: a
        soft-deleted category answers 404 here, which is what a reader
        expects from a row the tree does not show.
        """
        category = visible_categories().filter(slug=slug).first()
        if category is None:
            return StapelErrorResponse(
                404, ERR_404_SLUG_NOT_FOUND, params={"slug": slug}
            )

        response = Response(CategorySerializer(category).data)
        response["Cache-Control"] = (
            f"public, max-age={categories_settings.TREE_CACHE_TIMEOUT}"
        )
        return response

    @extend_schema(
        description="Get all deleted children of this category.",
        responses={200: CategorySerializer(many=True)},
        parameters=[],
    )
    @action(detail=True, methods=["get"], url_path="deleted-children", pagination_class=None)
    def deleted_children(self, request, pk=None):  # noqa: R007
        """Return deleted children of this category."""
        category = self.get_object()
        deleted_children = Category.objects.filter(tn_parent=category, deleted=True).order_by("name")
        return Response(CategorySerializer(deleted_children, many=True).data)  # noqa: R001

    @extend_schema(
        description="Restore deleted category and all its descendants.",
        responses={200: UndeleteResponseSerializer},
    )
    @action(detail=True, methods=["post"], url_path="undelete", permission_classes=[IsStaffUser])
    def undelete(self, request, pk=None):  # noqa: R007
        """Undelete category and all its descendants."""
        category = self.get_object()

        if not category.deleted:
            return StapelErrorResponse(400, ERR_400_CATEGORY_NOT_DELETED)

        descendants_pks = category.tn_descendants_pks
        descendant_ids = [int(dpk) for dpk in str(descendants_pks).split(",") if dpk]

        category.deleted = False
        category.save()

        for descendant in Category.objects.filter(id__in=descendant_ids):
            descendant.deleted = False
            descendant.save()

        dto = UndeleteResponse(restored=[category.pk] + descendant_ids)
        return Response(UndeleteResponseSerializer(dto).data, status=status.HTTP_200_OK)  # noqa: R001

    @extend_schema(
        description="Execute bulk commands on categories (add/edit/delete/reorder).",
        request=CategoryBulkCommandSerializer,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
    @action(detail=False, methods=["post"], url_path="bulk-commands", permission_classes=[IsStaffUser])
    def bulk_commands(self, request):  # noqa: R007
        """Execute bulk commands on categories."""
        serializer = CategoryBulkCommandSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        commands = serializer.validated_data.get("categories", [])
        results = {"created": [], "updated": [], "deleted": [], "errors": []}

        for cmd in commands:
            command_type = cmd["command"]
            category_id = cmd.get("id")

            try:
                if command_type == "add":
                    parent_id = cmd.get("parent_id")
                    parent = None
                    if parent_id:
                        try:
                            parent = Category.objects.get(pk=parent_id)
                        except Category.DoesNotExist:
                            results["errors"].append(
                                {"command": cmd, "error": f"Parent category {parent_id} not found"}
                            )
                            continue

                    category = Category.objects.create(
                        name=cmd["name"],
                        slug=cmd["slug"],
                        translatable=cmd.get("translatable", True),
                        tn_parent=parent,
                        tn_priority=cmd.get("priority", 0),
                    )
                    results["created"].append(category.pk)

                elif command_type == "edit":
                    try:
                        category = Category.objects.get(pk=category_id)
                        if "name" in cmd:
                            category.name = cmd["name"]
                        if "slug" in cmd:
                            category.slug = cmd["slug"]
                        if "translatable" in cmd:
                            category.translatable = cmd["translatable"]
                        category.save()
                        results["updated"].append(category.pk)
                    except Category.DoesNotExist:
                        results["errors"].append(
                            {"command": cmd, "error": f"Category {category_id} not found"}
                        )

                elif command_type == "delete":
                    try:
                        category = Category.objects.get(pk=category_id)
                        descendants_pks = category.tn_descendants_pks
                        descendant_ids = [int(dpk) for dpk in str(descendants_pks).split(",") if dpk]

                        category.deleted = True
                        category.tn_priority = 0
                        category.save()

                        Category.objects.filter(id__in=descendant_ids).update(deleted=True)

                        results["deleted"].append(category.pk)
                        results["deleted"].extend(descendant_ids)
                    except Category.DoesNotExist:
                        results["errors"].append(
                            {"command": cmd, "error": f"Category {category_id} not found"}
                        )

                elif command_type == "reorder":
                    try:
                        category = Category.objects.get(pk=category_id)
                        category.tn_priority = cmd.get("priority", 0)
                        category.save()
                        results["updated"].append(category.pk)
                    except Category.DoesNotExist:
                        results["errors"].append(
                            {"command": cmd, "error": f"Category {category_id} not found"}
                        )

                elif command_type == "keep":
                    pass

            except Exception as e:  # noqa: BLE001 — surface per-command errors, keep batch going
                results["errors"].append({"command": cmd, "error": str(e)})

        return Response(results, status=status.HTTP_200_OK)  # noqa: R001

    @extend_schema(
        operation_id="collect_translation_keys",
        summary="Collect all translation keys",
        description="Collect translation keys from categories, features and feature config options.",
        responses={200: OpenApiTypes.OBJECT},
    )
    @action(detail=False, methods=["get"], permission_classes=[IsServiceRequest], url_path="translation-keys")
    def translation_keys(self, request):  # noqa: R007
        """Collect all translation keys from catalog entities."""
        from .translation_keys import collect_all_catalog_translation_keys

        return Response(collect_all_catalog_translation_keys())  # noqa: R001


@extend_schema_view(
    retrieve=extend_schema(
        description="Get feature details with tree structure info.",
        responses={200: FeatureSerializer},
    ),
    create=extend_schema(
        description="Create a new feature with polymorphic config.",
        request=FeatureCreateUpdateSerializer,
        responses={201: FeatureSerializer},
    ),
    update=extend_schema(
        description="Update an existing feature with polymorphic config.",
        request=FeatureCreateUpdateSerializer,
        responses={200: FeatureSerializer},
    ),
    partial_update=extend_schema(
        description="Partially update a feature.",
        request=FeatureCreateUpdateSerializer,
        responses={200: FeatureSerializer},
    ),
)
@extend_schema(tags=["Features"])
class FeatureViewSet(RevisionViewSetMixin, viewsets.ModelViewSet):
    queryset = Feature.objects.all()
    permission_classes = [ReadOnlyOrStaff]
    pagination_class = RevisionPagination

    def get_serializer_class(self):
        if self.action in ["list"]:
            return FeatureCompactSerializer
        if self.action in ["create", "update", "partial_update"]:
            return FeatureCreateUpdateSerializer
        return FeatureSerializer

    @extend_schema(
        description="List features with revision-based pagination.",
        parameters=REVISION_PARAMETERS,
        responses={200: FeatureCompactSerializer},
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        description="Bulk create or update features. Provide an array of feature objects with IDs.",
        request=FeatureBulkSerializer(many=True),
        responses={200: BulkUpdateResponseSerializer, 400: OpenApiTypes.OBJECT},
    )
    @action(detail=False, methods=["post"], permission_classes=[IsStaffUser])
    def bulk_add(self, request):  # noqa: R007
        data = request.data
        if not isinstance(data, list):
            return StapelErrorResponse(400, ERR_400_EXPECTED_LIST)

        updated = []
        for item in data:
            item_id = item.get("id")
            if not item_id:
                continue

            parent_id = item.get("tn_parent")
            parent = None
            if parent_id:
                try:
                    parent = Feature.objects.get(pk=parent_id)
                except Feature.DoesNotExist:
                    parent = None

            defaults = {
                "name": item.get("name", ""),
                "slug": item.get("slug", ""),
                "tn_parent": parent,
                "tn_priority": item.get("tn_priority", 0),
                "comment": item.get("comment", ""),
                "icon": item.get("icon") or "",
                "mandatory": item.get("mandatory", False),
                "show_as_badge": item.get("show_as_badge", False),
                "show_at_title": item.get("show_at_title", False),
                "translate": item.get("translate", "all"),
                "config": item.get("config", {}),
            }

            obj, _created = Feature.objects.update_or_create(id=item_id, defaults=defaults)
            updated.append(obj.pk)

        reset_sequences_for_models(Feature)
        return Response({"updated_ids": updated}, status=status.HTTP_200_OK)  # noqa: R001

    @extend_schema(
        description="Convert feature type between select and string, optionally propagating to descendants.",
        request=inline_serializer(
            name="FeatureConvertType",
            fields={
                # Narrower than the full FeatureConfig union in practice (only
                # select<->string is a supported conversion,
                # ERR_400_INVALID_CONVERSION), but the FeatureConfig $ref is
                # still the honest upper bound — a bare DictField described
                # nothing at all.
                "config": FeatureConfigSchemaField(help_text="New config after conversion"),
                "propagate": drf_serializers.BooleanField(
                    required=False, default=False, help_text="Whether to propagate to all descendants"
                ),
            },
        ),
        responses={200: FeatureSerializer},
    )
    @action(detail=True, methods=["post"], url_path="convert-type", permission_classes=[IsStaffUser])
    def convert_type(self, request, pk=None):  # noqa: R007
        """Convert feature type between select and string, optionally propagating."""
        feature = self.get_object()
        new_config = request.data.get("config")
        propagate = request.data.get("propagate", False)

        if not new_config or not isinstance(new_config, dict):
            return StapelErrorResponse(400, ERR_400_CONFIG_REQUIRED)

        old_type = feature.config.get("type", "")
        new_type = new_config.get("type", "")

        valid_conversions = {("select", "string"), ("string", "select")}
        if (old_type, new_type) not in valid_conversions:
            return StapelErrorResponse(400, ERR_400_INVALID_CONVERSION)

        feature.config = new_config
        feature.save()

        if propagate:
            descendants_pks = feature.tn_descendants_pks
            if descendants_pks:
                descendant_ids = [int(dpk) for dpk in str(descendants_pks).split(",") if dpk.strip()]
                for descendant in Feature.objects.filter(id__in=descendant_ids):
                    desc_type = descendant.config.get("type", "")
                    if desc_type == old_type:
                        descendant.config = FeatureViewSet._convert_config(
                            descendant.config, old_type, new_type, descendant.slug
                        )
                        descendant.save()

        return Response(FeatureSerializer(feature).data)  # noqa: R001

    @staticmethod
    def _convert_config(config, from_type, to_type, slug=""):
        """Convert a single config between select and string types."""
        if from_type == "select" and to_type == "string":
            options = [opt.get("value", "") for opt in config.get("options", []) if opt.get("value")]
            return {"type": "string", "options": options, "allowCustom": True}
        elif from_type == "string" and to_type == "select":
            options = config.get("options", [])
            select_options = []
            for opt in options:
                label = f"feature.{slug}.{opt}" if slug else opt
                select_options.append({"value": opt, "label": label})
            return {
                "type": "select",
                "options": select_options,
                "uiStyle": "chips",
                "minSelected": 0,
                "maxSelected": None,
            }
        return config
