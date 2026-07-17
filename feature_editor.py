from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set

from django.db import transaction

from .models import Category, CategoryFeature, Feature
from .serializers import FeatureSerializer


# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------


class FeatureEditorError(ValueError):
    """A feature-editor request that violates a server-side invariant.

    Raised for rule violations the UI already blocks but the API must
    re-enforce (M-4: edit/remove of an inherited slug; L-9: cross-tree
    ``replace``; L-11: ``inherit`` slug not matching its source). The view
    maps it to HTTP 400.
    """


class FeatureEditorConflict(Exception):
    """Optimistic-concurrency clash: the editor's base revision is stale.

    Raised when ``apply_feature_editor_changes`` is given a ``base_revision``
    that no longer matches the category's current revision — another editor
    committed in between (M-5). The view maps it to HTTP 409 so the client
    reloads and re-applies against fresh state instead of silently clobbering
    the other edit.
    """

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"stale base_revision {expected}; category is now at revision {actual}"
        )


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class FeatureEditorItem:
    """Normalized editor item produced by serializers."""

    action: str  # keep, add, edit, inherit, remove, create, replace
    order: int
    feature: Dict
    replace_with: Optional[int] = None  # Feature ID to replace with (for replace action)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iter_children(category: Category) -> Iterable[Category]:
    """Return direct children using available helpers or fallback query."""
    if hasattr(category, "get_children_queryset"):
        return category.get_children_queryset()
    if hasattr(category, "get_children"):
        return category.get_children()
    return Category.objects.filter(tn_parent=category)


def _iter_descendants(category: Category) -> List[Category]:
    """Collect descendants via BFS without depending on treenode internals."""
    descendants: List[Category] = []
    queue: List[Category] = list(_iter_children(category))
    while queue:
        node = queue.pop(0)
        descendants.append(node)
        queue.extend(_iter_children(node))
    return descendants


def _rewrite_orders(category: Category, ordered_features: List[Feature]) -> None:
    """Rewrite CategoryFeature rows for category to match ordered_features."""
    existing = {
        cf.feature_id: cf
        for cf in CategoryFeature.objects.filter(category=category).select_related("feature")
    }
    seen_ids: Set[int] = set()
    for idx, feature in enumerate(ordered_features):
        seen_ids.add(feature.pk)
        cf = existing.get(feature.pk)
        if cf:
            if cf.order != idx:
                cf.order = idx
                cf.save(update_fields=["order"])
        else:
            CategoryFeature.objects.create(category=category, feature=feature, order=idx)
    # Remove stale rows that are not part of the ordered set
    stale_ids = [fid for fid in existing.keys() if fid not in seen_ids]
    if stale_ids:
        CategoryFeature.objects.filter(category=category, feature_id__in=stale_ids).delete()


def _remove_slug_recursive(category: Category, slug: str) -> Set[int]:
    """Remove CategoryFeature by slug for category and descendants; return changed category IDs."""
    changed: Set[int] = set()
    targets = [category] + _iter_descendants(category)
    for cat in targets:
        deleted, _ = CategoryFeature.objects.filter(category=cat, feature__slug=slug).delete()
        if deleted:
            changed.add(cat.pk)
    return changed


def _insert_feature_with_after_slug(
    category: Category, feature: Feature, after_slug: Optional[str]
) -> None:
    """
    Insert feature into category after a feature with slug=after_slug.

    If after_slug is None, place at start. If no match, append to the end.
    Rewrites orders but keeps other features untouched.
    """
    links = list(
        CategoryFeature.objects.filter(category=category)
        .select_related("feature")
        .order_by("order", "id")
    )
    ordered_features: List[Feature] = [
        cf.feature
        for cf in links
        if cf.feature
        and cf.feature.slug != feature.slug
    ]

    inserted = False
    if after_slug is None:
        ordered_features = [feature] + ordered_features
        inserted = True
    else:
        new_order: List[Feature] = []
        for feat in ordered_features:
            new_order.append(feat)
            if not inserted and feat.slug == after_slug:
                new_order.append(feature)
                inserted = True
        if not inserted:
            new_order.append(feature)
        ordered_features = new_order

    _rewrite_orders(category, ordered_features)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_editor_state(category: Category) -> Dict:
    """
    Build editor JSON with available actions and root feature choices.
    """
    parent = category.tn_parent
    parent_features_by_slug: Dict[str, Feature] = {}
    if parent:
        for link in (
            CategoryFeature.objects.filter(category=parent)
            .select_related("feature")
            .order_by("order", "id")
        ):
            if link.feature and link.feature.slug:
                parent_features_by_slug[link.feature.slug] = link.feature

    items = []
    current_slugs: Set[str] = set()
    for link in (
        CategoryFeature.objects.filter(category=category)
        .select_related("feature")
        .order_by("order", "id")
    ):
        feature = link.feature
        if not feature:
            continue
        slug = feature.slug or ""
        current_slugs.add(slug)
        # keep, inherit доступны всегда. edit и remove - только если у парента нет характеристик с таким slug
        available_actions = ["keep", "inherit"]
        if slug not in parent_features_by_slug:
            available_actions.extend(["edit", "remove"])

        item = {
            "order": link.order,
            "available_actions": available_actions,
            "action": "keep",
            "feature": FeatureSerializer(feature).data,
        }
        parent_feature = parent_features_by_slug.get(slug)
        if parent_feature:
            item["parent_feature"] = FeatureSerializer(parent_feature).data
        items.append(item)

    available_root_features = Feature.objects.filter(tn_parent__isnull=True)
    if current_slugs:
        available_root_features = available_root_features.exclude(slug__in=list(current_slugs))

    return {
        "features": items,
        "available_root_features": FeatureSerializer(
            available_root_features.order_by("name"), many=True
        ).data,
        "draft": category.draft or "",
        # Echo the current revision so the client can send it back as
        # base_revision on apply for the optimistic-concurrency check (M-5).
        "revision": category.revision,
    }


@transaction.atomic
def apply_feature_editor_changes(
    category: Category,
    items: List[FeatureEditorItem],
    base_revision: int,
) -> None:
    """
    Apply editor actions to category and propagate to descendants following rules:

    Actions:
    - keep: no changes to Feature model, only update M2M order
    - add: add existing root Feature to M2M (no Feature model changes)
    - edit: update Feature model fields (only if not inherited from parent)
    - inherit: remove old Feature from M2M, create new Feature with old as parent, add to M2M
    - remove: remove Feature from M2M (recursively for descendants)
    - create: create new root Feature and add to M2M

    Concurrency (M-5): the category and its whole subtree are row-locked
    (``select_for_update``) at the top of the transaction so two applies
    serialize instead of interleaving lost updates / half-propagated trees.
    ``base_revision`` (echoed from the editor state's ``revision``) must match
    the category's current revision, else :class:`FeatureEditorConflict` (the
    caller applied against stale editor state).

    Algorithm:
    1. Validate the whole batch (server-side action rules) before any write
    2. Process removes (and the remove part of inherit) for current category and descendants
    3. Process adds/inherit/create additions for current category and descendants
    4. Update order for current category only
    """
    if not items:
        return

    # M-5: lock the category and its whole subtree up-front. Deterministic
    # order (by pk) avoids deadlocks between two applies over overlapping
    # trees; select_for_update is a no-op on backends without row locking.
    descendant_pks = [d.pk for d in _iter_descendants(category)]
    lock_ids = [category.pk] + descendant_pks
    list(Category.objects.select_for_update().filter(pk__in=lock_ids).order_by("pk"))

    # M-5: optimistic-concurrency guard. Read the current revision under the
    # lock and reject a stale editor before mutating anything.
    current_revision = Category.objects.values_list("revision", flat=True).get(
        pk=category.pk
    )
    if current_revision != int(base_revision):
        raise FeatureEditorConflict(int(base_revision), current_revision)

    # Sort items by requested order once
    ordered_items = sorted(items, key=lambda it: it.order)

    # --- Validation pass (before any mutation) --------------------------------
    # Mirror the UI's available_actions rule server-side (M-4) plus the
    # source-consistency rules the create/inherit paths bypass (L-9, L-11).
    parent = category.tn_parent
    parent_slugs: Set[str] = set()
    if parent:
        for link in (
            CategoryFeature.objects.filter(category=parent).select_related("feature")
        ):
            if link.feature and link.feature.slug:
                parent_slugs.add(link.feature.slug)

    for item in ordered_items:
        action = item.action
        payload = item.feature or {}
        slug = (payload.get("slug") or "").strip()
        feature_id = payload.get("id")

        # M-4: edit/remove is only offered for a slug the parent does NOT
        # carry; an inherited (shared) slug must be re-shaped via inherit,
        # never edited/removed from a child.
        if action in ("edit", "remove") and slug and slug in parent_slugs:
            raise FeatureEditorError(
                f"action '{action}' is not allowed for inherited slug '{slug}'"
            )

        # L-11: an inherit child must keep its source feature's slug, else the
        # stage-1 remove (keyed by payload slug) misses and the category ends
        # up with two versions of one root.
        if action == "inherit" and feature_id:
            source_slug = (
                Feature.objects.filter(pk=feature_id)
                .values_list("slug", flat=True)
                .first()
            )
            if source_slug and slug and slug != source_slug:
                raise FeatureEditorError(
                    f"inherit slug '{slug}' must match source feature slug "
                    f"'{source_slug}'"
                )

        # L-9: replace only swaps in another version from the SAME feature tree
        # (shared root); a missing replacement is an error, not a silent no-op.
        if action == "replace" and item.replace_with:
            replacement = Feature.objects.filter(pk=item.replace_with).first()
            if replacement is None:
                raise FeatureEditorError(
                    f"replace_with feature {item.replace_with} not found"
                )
            if feature_id:
                original = Feature.objects.filter(pk=feature_id).first()
                if original is not None and original.root_pk != replacement.root_pk:
                    raise FeatureEditorError(
                        "replace_with must belong to the same feature tree as "
                        "the feature it replaces"
                    )

    # Build prev-slug map (ignoring removed items) for positioning in descendants
    prev_slug_map: Dict[str, Optional[str]] = {}
    prev_slug: Optional[str] = None
    for item in ordered_items:
        if item.action == "remove":
            continue
        slug = (item.feature.get("slug") or "").strip()
        prev_slug_map[slug] = prev_slug
        prev_slug = slug

    changed_categories: Set[int] = set()

    # Stage 1: removals (inherit performs removal first, remove removes)
    for item in ordered_items:
        if item.action in ("remove", "inherit"):
            slug = (item.feature.get("slug") or "").strip()
            changed_categories.update(_remove_slug_recursive(category, slug))

    # Stage 2: apply actions and prepare final list
    final_features: List[Feature] = []
    additions: List[Dict] = []  # entries: {feature, slug, after_slug}

    for item in ordered_items:
        action = item.action
        payload = item.feature or {}
        slug = (payload.get("slug") or "").strip()
        feature_id = payload.get("id")

        if action == "remove":
            # Already handled in stage 1, skip
            continue

        elif action == "keep":
            # No changes to Feature, just include in final order
            if feature_id:
                try:
                    feature_obj = Feature.objects.get(pk=feature_id)
                    final_features.append(feature_obj)
                except Feature.DoesNotExist:
                    pass

        elif action == "add":
            # Add existing root Feature to M2M (no Feature model changes)
            if feature_id:
                try:
                    feature_obj = Feature.objects.get(pk=feature_id)
                    final_features.append(feature_obj)
                    additions.append({
                        "feature": feature_obj,
                        "slug": slug,
                        "after_slug": prev_slug_map.get(slug),
                    })
                except Feature.DoesNotExist:
                    pass

        elif action == "edit":
            # Update Feature model fields. Go through Feature.save() (NOT a
            # QuerySet.update()) so the edit bumps the feature's revision,
            # fans out category.changed to EVERY category carrying it
            # (emit_category_changed_on_feature_save), refreshes the cached
            # translation and validates the config — all bypassed by .update()
            # (H-2). ``icon`` is carried too, instead of silently dropped
            # (L-10).
            if feature_id:
                try:
                    feature_obj = Feature.objects.get(pk=feature_id)
                except Feature.DoesNotExist:
                    continue
                feature_obj.name = payload.get("name", "")
                feature_obj.comment = payload.get("comment", "")
                feature_obj.config = payload.get("config", {}) or {}
                feature_obj.mandatory = payload.get("mandatory", False)
                feature_obj.show_as_badge = payload.get("show_as_badge", False)
                feature_obj.show_at_title = payload.get("show_at_title", False)
                feature_obj.translate = payload.get("translate", "all")
                if "icon" in payload:
                    feature_obj.icon = payload.get("icon") or ""
                # Validate the config (and slug rules) via the model's clean()
                # — the .update() path skipped this, letting a typeless config
                # land in the DB (L-10). Raises ValidationError -> 400.
                feature_obj.clean()
                feature_obj.save()
                final_features.append(feature_obj)

        elif action == "inherit":
            # Create new Feature with old feature as parent
            # The old feature's ID is used as tn_parent_id for the new feature
            if feature_id:
                feature_obj = Feature.objects.create(
                    tn_parent_id=feature_id,  # Old feature becomes parent
                    name=payload.get("name", ""),
                    slug=slug,
                    icon=payload.get("icon") or "",
                    comment=payload.get("comment", ""),
                    config=payload.get("config", {}) or {},
                    mandatory=payload.get("mandatory", False),
                    show_as_badge=payload.get("show_as_badge", False),
                    show_at_title=payload.get("show_at_title", False),
                    translate=payload.get("translate", "all"),
                    tn_priority=payload.get("tn_priority", 0),
                )
                final_features.append(feature_obj)
                additions.append({
                    "feature": feature_obj,
                    "slug": slug,
                    "after_slug": prev_slug_map.get(slug),
                })

        elif action == "create":
            # Create new root Feature (tn_parent=null)
            feature_obj = Feature.objects.create(
                tn_parent=None,
                name=payload.get("name", ""),
                slug=slug,
                icon=payload.get("icon") or "",
                comment=payload.get("comment", ""),
                config=payload.get("config", {}) or {},
                mandatory=payload.get("mandatory", False),
                show_as_badge=payload.get("show_as_badge", False),
                show_at_title=payload.get("show_at_title", False),
                translate=payload.get("translate", "all"),
                tn_priority=payload.get("tn_priority", 0),
            )
            final_features.append(feature_obj)
            additions.append({
                "feature": feature_obj,
                "slug": slug,
                "after_slug": prev_slug_map.get(slug),
            })

        elif action == "replace":
            # Replace current feature with another version from the SAME feature
            # tree. Existence + same-tree are enforced in the validation pass
            # (L-9), so no silent fallback here. M2M-only; not propagated to
            # descendants (each category picks its own version).
            replace_with_id = item.replace_with
            if replace_with_id:
                new_feature = Feature.objects.get(pk=replace_with_id)
                final_features.append(new_feature)

    # Stage 2b: propagate additions to descendants (add, inherit, create)
    for addition in additions:
        feature_obj = addition["feature"]
        after_slug = addition.get("after_slug")
        for descendant in _iter_descendants(category):
            _insert_feature_with_after_slug(descendant, feature_obj, after_slug)
            changed_categories.add(descendant.pk)

    # Stage 3: rewrite current category ordering to exactly match incoming order
    _rewrite_orders(category, final_features)
    changed_categories.add(category.pk)

    # Touch categories to bump revision for sync
    for cat in Category.objects.filter(pk__in=changed_categories):
        cat.save()
