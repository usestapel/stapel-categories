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
    OpenApiParameter,
    extend_schema,
    extend_schema_view,
    inline_serializer,
)
from rest_framework import serializers as drf_serializers
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

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
from .effective import EFFECTIVE_FROM_CHILDREN, effective_features
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
from .models import Category, Feature, resolve_children_as
from .serializers import (
    CategoryBulkCommandSerializer,
    CategoryBulkSerializer,
    CategorySerializer,
    CategoryStaffSerializer,
    CategoryTreeNodeSerializer,
    FeatureBulkSerializer,
    FeatureCompactSerializer,
    FeatureConfigSchemaField,
    FeatureCreateUpdateSerializer,
    FeatureEditorApplySerializer,
    FeatureEditorDraftResponseSerializer,
    FeatureEditorDraftSerializer,
    FeatureEditorStateSerializer,
    FeatureEffectiveSerializer,
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


def structural_ancestor_ids() -> set[int]:
    """Ids of the rows an ACTIVE category still hangs from, retired or not.

    The small half of :func:`visible_categories`'s rule, and the reason it
    can be a rule at all: django-treenode denormalises every row's ancestry
    into ``tn_ancestors_pks``, so "which retired rows are still holding
    something live up" is one column read over the active set, not a tree
    walk per row.

    Cached on the same revision+row-count fingerprint the roots cache uses.
    Any write to any category bumps ``revision``, so the key moves with the
    tree and there is no invalidation to remember.
    """
    key = f"{_ROOTS_CACHE_PREFIX}anc:{_roots_cache_key()}"
    cached = cache.get(key)
    if cached is not None:
        return set(cached)

    from treenode.utils import split_pks

    ancestors: set[int] = set()
    rows = Category.objects.filter(deleted=False, active=True).values_list(
        "tn_ancestors_pks", flat=True
    )
    for pks in rows:
        for pk in split_pks(pks):
            try:
                ancestors.add(int(pk))
            except (TypeError, ValueError):  # pragma: no cover - defensive
                continue
    cache.set(key, sorted(ancestors), categories_settings.CAROUSEL_CACHE_TIMEOUT)
    return ancestors


def visible_categories():
    """The rows the public tree reads are allowed to return.

    ONE definition, called by ``children``, ``roots`` and ``by-slug``, so the
    three cannot drift into showing different catalogues. That drift is not
    hypothetical: two of these are new, and the obvious way to write them is
    to copy the filter out of ``children`` — which works right up until
    somebody changes one of the copies.

    What it filters, and what it deliberately does not:

    * ``active`` is filtered **only where hiding the row opens no hole**. A
      retired category that an active one still hangs from is served, and
      the serializer ships ``active`` so a client can grey it out — take it
      away and the tree denies a parent whose child it still offers. A
      retired category with nothing live beneath it structures nothing, and
      is gone.

      Until 0.16.0 the first half of that sentence was applied to both, and
      a live stand's public feed served 174 rows named ``smoke-1787331903``,
      ``authz-1787369370``, ``storefront-…`` — every acceptance run the
      fleet had ever done, to anyone with curl. They were already
      ``active=False``; "inactive rows are structural" was being applied to
      rows that structure nothing. Retiring a category is how an operator
      takes it out of the catalogue, and it now does that.

    * ``is_test`` is **not** filtered, for the reason the model states at
      its declaration: it is an *export* filter (``export_catalog`` excludes
      it from committed fixtures), not a runtime-visibility gate. The advice
      that used to sit here — "a deployment that wants test rows hidden
      hides them with ``active``" — could not work while ``active`` was not
      a gate, which is how the 174 rows survived every sweep that took it.
      It works now. (``is_test`` remains a field with no writer in this
      fleet; nothing sets it, so nothing should be built on it.)

    * ``deleted=False`` — a soft-deleted category is gone as far as any
      reader is concerned. ``deleted-children`` is the staff view that asks
      for them on purpose.

    Changing any of these is a change to every read at once, which is the
    point of it being one function — and since 0.16.0 that includes the flat
    LIST, which used to answer the sync contract to strangers.
    """
    from django.db.models import Q

    return Category.objects.filter(deleted=False).filter(
        Q(active=True) | Q(pk__in=structural_ancestor_ids())
    )


def with_live_children(queryset):
    """Attach each row's LIVE children in ONE query.

    ``Category.live_children`` is the reader's own child list (this module's
    visibility rule, not treenode's denormalised columns, which count
    soft-deleted and retired rows). Without this a page of N categories would
    ask for it N times; with it, once for the page — which is what keeps
    ``children_pks``/``children_count`` and ``children_as`` free to serve.

    The prefetch fills ``_live_children``, not ``live_children``: the latter
    is the property that reads it, and a property cannot be assigned over.
    """
    from django.db.models import Prefetch

    return queryset.prefetch_related(
        Prefetch(
            "tn_children",
            queryset=visible_categories().order_by("-tn_priority", "id"),
            to_attr="_live_children",
        )
    )


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
    # `features` is a plain PK list on the public projection, and without the
    # prefetch DRF asks for it once per row — so every read of this viewset
    # cost one query per category served. Declared on the class attribute so
    # the list, the detail and every @action that starts from
    # `self.get_queryset()` inherit it rather than each remembering.
    queryset = Category.objects.all().prefetch_related("features")
    permission_classes = [ReadOnlyOrStaff]
    pagination_class = RevisionPagination

    @staticmethod
    def is_sync_reader(request) -> bool:
        """Whether *request* is entitled to the SYNC feed, not the catalogue.

        Two principals, and only two: a fleet service (``X-API-KEY``, the
        fleet's one word for "not a person") and staff. Both are operating
        the catalogue rather than shopping it.
        """
        user = getattr(request, "user", None)
        return bool(
            getattr(request, "is_service_request", False)
            or (user is not None and getattr(user, "is_staff", False))
        )

    def get_queryset(self):
        """Two readers on one URL, told apart (Д88).

        The flat list is the revision-SYNC feed, and a sync feed MUST serve
        retired rows: a consumer that cannot see a retirement cannot apply
        it, and would keep a dead category forever. The same URL is also the
        catalogue a storefront walks, unauthenticated, and on a live stand
        the sync contract won — 174 rows named ``smoke-1787331903``,
        ``authz-1787369370``, ``storefront-…`` served to anyone with curl.

        Fixed by splitting the READERS, not by deleting the rows: they are
        legitimately inactive and a syncing consumer is legitimately
        entitled to them. A sync principal gets what it always got, down to
        ``include_deleted``. Everyone else gets ``visible_categories()`` —
        the same catalogue the three tree reads serve, which is the other
        half of the defect: the list never shared that definition, so the
        two doors answered differently about the same tree.

        Note for a consumer that only ever syncs INCREMENTALLY: convergence
        on retirements lives on the authenticated feed. A public reader is
        served a catalogue, and re-walks it (which is what this fleet's
        storefront does — a full paginated walk pinned to one
        ``max_revision``, every session).
        """
        # `with_live_children` on both branches: the child fingerprint a
        # reader is served is the LIVE one whichever door it came through,
        # and a sync consumer that walks the whole table still gets it in one
        # query rather than one per row.
        queryset = with_live_children(super().get_queryset())
        if self.action != "list" or self.is_sync_reader(self.request):
            return queryset
        return queryset.filter(pk__in=visible_categories().values("pk"))

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
            queryset = with_live_children(
                Category.objects.filter(
                    active=True, carousel_enabled=True, deleted=False
                ).prefetch_related("features").order_by("-tn_priority")
            )
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
        description=(
            "Get all features for this category, sorted by order. Includes "
            "inherited features. For a `chips` parent that declares no "
            "features of its own the answer is the EFFECTIVE schema — the "
            "intersection of its children's, since the parent renders the "
            "feed and the chip row for all of them; a feature only some "
            "children carry appears once its chip is picked, and one whose "
            "children disagree carries `divergent: true` beside the widest "
            "config of theirs. The `X-Effective-From: children` response "
            "header says the list was intersected rather than read off this "
            "node (`own` otherwise)."
        ),
        responses={200: FeatureEffectiveSerializer(many=True)},
        parameters=[
            OpenApiParameter(
                name="X-Effective-From",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.HEADER,
                response=[200],
                enum=["own", "children"],
                description=(
                    "`own` — the schema is this node's own (own + "
                    "inherited). `children` — this node is a `chips` parent "
                    "declaring nothing itself, so the list is the "
                    "intersection of its children's schemas."
                ),
            )
        ],
    )
    @action(detail=True, methods=["get"], url_path="features", pagination_class=None)
    def category_features(self, request, pk=None):  # noqa: R007
        """Return full feature objects for this category, sorted by order."""
        category = self.get_object()
        features, source = effective_features(category)
        # The body stays a bare ARRAY — every client of this endpoint reads
        # one — so the one piece of meta rides as a header rather than
        # wrapping the list in an envelope nobody could read yet.
        serializer = (
            FeatureEffectiveSerializer
            if source == EFFECTIVE_FROM_CHILDREN
            else FeatureCompactSerializer
        )
        response = Response(serializer(features, many=True).data)  # noqa: R001
        response["X-Effective-From"] = source
        return response

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
        children = with_live_children(
            visible_categories()
            .filter(tn_parent=category)
            .prefetch_related("features")
            .order_by("-tn_priority", "id")
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
            queryset = with_live_children(
                visible_categories()
                .filter(tn_parent__isnull=True)
                .prefetch_related("features")
                .order_by("-tn_priority", "id")
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
        deleted_children = with_live_children(
            Category.objects.filter(tn_parent=category, deleted=True).order_by("name")
        )
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


#: Cache key prefix of the nested tree response, completed by the depth and
#: the same tree fingerprint the roots cache uses.
_TREE_CACHE_PREFIX = "stapel_categories:tree:"

#: Deepest tree a single call will assemble. Four levels is the mega-menu
#: (roots -> section headers -> links) plus one, and the cap is what keeps
#: "?depth=" from being a way to ask for the whole catalogue nested.
TREE_MAX_DEPTH = 4
#: What a caller gets for saying nothing — the three levels the desktop
#: mega-menu renders.
TREE_DEFAULT_DEPTH = 3


def _tree_depth_param(raw) -> int:
    """Read ``?depth=``: clamped to 1..:data:`TREE_MAX_DEPTH`, never an error.

    Clamped rather than rejected because every out-of-range answer this
    endpoint could give is still a correct prefix of the tree the caller
    asked for, and a 400 here would put a new code in the module's error
    catalogue to say "I gave you less than you asked for".
    """
    if raw in (None, ""):
        return TREE_DEFAULT_DEPTH
    try:
        depth = int(raw)
    except (TypeError, ValueError):
        return TREE_DEFAULT_DEPTH
    return max(1, min(depth, TREE_MAX_DEPTH))


def build_category_tree(depth: int) -> list[dict]:
    """The visible catalogue, nested, down to *depth* levels.

    TWO flat reads whatever the depth: django-treenode denormalises ancestry
    onto every row, so the visible set comes back as one ``values()`` read and
    the nesting is done in Python; the second is a grouped count of LIVE
    children per parent over the WHOLE visible set. The alternative — a
    queryset per level, or a nested serializer over model instances — is the
    N+1 this endpoint exists to remove from the storefront.

    The second read is not the first one counted: at the depth cap a node's
    children are real but unsent, and ``children_count`` / ``children_as``
    have to say so. treenode's own ``tn_children_count`` would answer without
    a query and answer WRONG — it counts soft-deleted and retired rows, which
    is how a services root reported three children while ``/children/``
    returned one.

    Visibility is :func:`visible_categories`, the same rule ``roots``,
    ``children`` and ``by-slug`` answer to, and the order is theirs as well
    (``-tn_priority``, then id) at every level.

    A node whose parent is not in the visible set is dropped rather than
    re-parented: the tree a client walks must be the tree the other reads
    hand back, and promoting an orphan would invent a root the catalogue
    does not have.
    """
    from django.db.models import Count

    rows = list(
        visible_categories()
        .filter(tn_level__lte=depth)
        .order_by("-tn_priority", "id")
        .values(
            "id", "slug", "name", "catalog_icon",
            "children_as", "children_as_derived", "children_axis_label",
            "tn_parent_id", "tn_ancestors_pks",
        )
    )
    live_children = {
        row["tn_parent_id"]: row["n"]
        for row in visible_categories()
        .exclude(tn_parent_id=None)
        .values("tn_parent_id")
        .annotate(n=Count("pk"))
    }

    nodes: dict[int, dict] = {}
    for row in rows:
        ancestors = [pk for pk in (row["tn_ancestors_pks"] or "").split(",") if pk]
        nodes[row["id"]] = {
            "id": row["id"],
            "slug": row["slug"],
            # The stored value, exactly as every other public read serves it
            # — this module ships translation KEYS and the client resolves
            # them; translating here would make one read speak differently.
            "name": row["name"],
            # The `category` parameter of a search query is this exact string.
            "path": "/".join([*ancestors, str(row["id"])]),
            "catalog_icon": row["catalog_icon"],
            "children_as": resolve_children_as(
                row["children_as"],
                row["children_as_derived"],
                bool(live_children.get(row["id"], 0)),
            ),
            # The stored key, like `name` above — a caption for the chip row
            # the level below is drawn as, "" when nobody named the axis.
            "children_axis_label": row["children_axis_label"],
            # How many children this node HAS, not how many this read sent:
            # at the depth cap `children` is empty and this is what tells a
            # menu there is another level to ask for.
            "children_count": live_children.get(row["id"], 0),
            "children": [],
        }

    roots: list[dict] = []
    for row in rows:
        node = nodes[row["id"]]
        parent = nodes.get(row["tn_parent_id"])
        if row["tn_parent_id"] is None:
            roots.append(node)
        elif parent is not None:
            parent["children"].append(node)
    return roots


@extend_schema(tags=["Categories"])
class CategoryTreeView(APIView):
    """``GET /categories/api/v1/tree/?depth=N`` — the catalogue in one call.

    The desktop mega-menu needs three levels at once. Assembled from
    ``roots`` plus one ``children`` call per node it would be one request per
    branch on the coldest page of the storefront; assembled from the flat
    list it is the whole table over the wire. This is one query, one cached
    response, and the four keys a menu renders (plus ``children_as``, which
    says whether the level below is a destination or a filter).

    Cached on the tree's own revision fingerprint rather than a TTL alone
    (see :func:`_roots_cache_key`): a catalogue edit retires the entry
    immediately, and ``TREE_CACHE_TIMEOUT`` is only the ceiling on how long
    an UNCHANGED tree keeps one.
    """

    permission_classes = [ReadOnlyOrStaff]

    @extend_schema(
        description=(
            "The active category tree, nested, ordered by `tn_priority` "
            "descending at every level. One call, one query, cached."
        ),
        parameters=[
            OpenApiParameter(
                name="depth",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                description=(
                    f"Levels to return, 1..{TREE_MAX_DEPTH} "
                    f"(default {TREE_DEFAULT_DEPTH}). Out-of-range values are "
                    "clamped, not refused."
                ),
            )
        ],
        responses={200: CategoryTreeNodeSerializer(many=True)},
    )
    def get(self, request):
        depth = _tree_depth_param(request.query_params.get("depth"))
        cache_key = (
            f"{_TREE_CACHE_PREFIX}{depth}:"
            f"{_roots_cache_key()[len(_ROOTS_CACHE_PREFIX):]}"
        )
        cached_data = cache.get(cache_key)
        if cached_data is None:
            cached_data = build_category_tree(depth)
            cache.set(
                cache_key, cached_data, timeout=categories_settings.TREE_CACHE_TIMEOUT
            )

        response = Response(cached_data)
        response["Cache-Control"] = (
            f"public, max-age={categories_settings.TREE_CACHE_TIMEOUT}"
        )
        return response
