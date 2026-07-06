"""Byte-stable catalog fixture serialization — export side (CAT-1).

Turns the live ``Category`` / ``Feature`` / ``CategoryFeature`` tables into
natural-key JSON fixtures that live in the *host project* repo
(``<BASE_DIR>/fixtures/catalog/``), are reviewed as code, and are reconciled
back into a DB by ``load_catalog`` (CAT-2, a separate task). Only the export
direction and the shared byte-stable/canonical helpers live here.

Design: ``docs/catalog-fixtures-sync.md``. Key decisions realized here:

* **Natural keys.** ``Category.slug`` (globally unique) and the *root*
  ``Feature.slug`` (unique among roots) are the portable identities; DB pks
  are never written. Parents are addressed by ``parent_slug``.
* **Materialized feature lists (§2).** Each category carries its *actually
  materialized* ordered feature list (what its ``CategoryFeature`` rows hold
  today — ``copy_parent_features`` stores copies, not lazy inheritance), not
  a diff from its parent. A list entry is either a bare ``{"slug": ...}``
  reference to a shared/root feature, or an inline override
  (``{"slug", "config", "mandatory", "show_as_badge", "show_at_title",
  "translate"}``) when the linked row is a tree override (``tn_parent`` set).
* **Override owner heuristic (§2).** When one override row is propagated to a
  category + its descendants, several categories reference it. We deliberately
  do *not* pick an "owning" category: every referencing category inlines its
  config independently (duplication accepted, no compression). No natural key
  is invented for override rows.
* **``is_test`` exclusion is transitive (§5).** A test category or feature is
  dropped, and so is any ``CategoryFeature`` link touching a test row —
  realized as a query/loop filter, not post-processing. ``--include-test``
  overrides this for local inspection only.
* **Byte-stable (§6).** Sorted keys, ``indent=2``, ``ensure_ascii=False``,
  trailing newline, deterministic record order — identical DB state yields
  byte-identical files (the ``dump_translations`` / codegen contract). No
  timestamps/UUIDs in file bodies; provenance lives in the git commit.
"""
import hashlib
import json

# Fixture directory (relative to the host project's BASE_DIR) and file names.
FIXTURE_DIRNAME = "catalog"
FEATURES_FILE = "features.json"
CATEGORIES_FILE = "categories.json"
STATE_FILE = ".sync-state.json"

# Sidecar schema version — bump if the sidecar shape changes so CAT-2 can
# detect an incompatible base.
STATE_VERSION = 1


def canonical_json(obj) -> str:
    """Byte-stable JSON text for a fixture file (trailing newline included)."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def content_hash(record) -> str:
    """Content hash of one canonical record, for the ``.sync-state`` sidecar.

    Compact (no indentation) sorted-key encoding so the hash is stable
    regardless of the pretty-printing used in the human-readable files.
    """
    blob = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _feature_record(feature, include_test: bool) -> dict:
    """Canonical (root) feature definition for ``features.json``."""
    rec = {
        "slug": feature.slug,
        "name": feature.name,
        "icon": feature.icon,
        "comment": feature.comment,
        "config": feature.config or {},
        "mandatory": feature.mandatory,
        "show_as_badge": feature.show_as_badge,
        "show_at_title": feature.show_at_title,
        "translate": feature.translate,
    }
    # is_test is only ever written under --include-test (default export filters
    # test rows out entirely, so this key never appears in a committed fixture).
    if include_test and feature.is_test:
        rec["is_test"] = True
    return rec


def _feature_list_entry(feature, include_test: bool) -> dict:
    """One entry in a category's materialized feature list.

    Bare ``{"slug"}`` reference for a shared root feature; inline config for a
    tree override (``tn_parent`` set). Slug-less rows (e.g. ``header`` display
    rows) have no natural key, so they are inlined self-contained (name/icon/
    comment carried too) rather than silently dropped.
    """
    is_override = feature.tn_parent_id is not None
    slug = feature.slug or ""

    if not is_override and slug:
        entry = {"slug": slug}
    else:
        entry = {
            "slug": slug,
            "config": feature.config or {},
            "mandatory": feature.mandatory,
            "show_as_badge": feature.show_as_badge,
            "show_at_title": feature.show_at_title,
            "translate": feature.translate,
        }
        if not slug:
            # No canonical (features.json) home for the display attributes —
            # keep them on the inline entry so nothing is lost.
            entry["name"] = feature.name
            entry["icon"] = feature.icon
            entry["comment"] = feature.comment
    if include_test and feature.is_test:
        entry["is_test"] = True
    return entry


def _category_record(category, include_test: bool) -> dict:
    """Category record: tree edge + materialized feature list."""
    parent = category.tn_parent
    features = []
    links = (
        category.category_features.all()
        .order_by("order", "id")
        .select_related("feature")
    )
    for link in links:
        feature = link.feature
        if feature is None or feature.deleted:
            continue
        # Transitive is_test exclusion: drop the link if the feature end is a
        # test row (the category end is already filtered by the caller).
        if not include_test and feature.is_test:
            continue
        features.append(_feature_list_entry(feature, include_test))

    rec = {
        "slug": category.slug,
        "parent_slug": parent.slug if parent is not None else None,
        "name": category.name,
        "comment": category.comment,
        "catalog_icon": category.catalog_icon,
        "carousel_icon": category.carousel_icon,
        "carousel_enabled": category.carousel_enabled,
        "active": category.active,
        "translatable": category.translatable,
        "features": features,
    }
    if include_test and category.is_test:
        rec["is_test"] = True
    return rec


def build_catalog(include_test: bool = False):
    """Build the natural-key representation of the live catalog.

    Returns ``(features, categories, state)`` where ``features``/``categories``
    are the sorted record lists and ``state`` is the ``.sync-state`` sidecar
    payload (content-hash per natural key + max revision for the pre-filter).

    Soft-deleted rows (``deleted=True``) are omitted — a fixture describes the
    desired *live* catalog; a removal is expressed by absence. ``is_test`` rows
    are omitted unless ``include_test``.
    """
    from .models import Category, Feature

    # --- Root feature definitions (features.json) ---------------------------
    feature_qs = Feature.objects.filter(
        tn_parent__isnull=True, deleted=False
    ).exclude(slug="")
    if not include_test:
        feature_qs = feature_qs.filter(is_test=False)

    feature_records = [_feature_record(f, include_test) for f in feature_qs]
    feature_records.sort(key=lambda r: r["slug"])

    # --- Categories (categories.json) ---------------------------------------
    category_qs = Category.objects.filter(deleted=False).select_related("tn_parent")
    if not include_test:
        category_qs = category_qs.filter(is_test=False)

    category_records = [_category_record(c, include_test) for c in category_qs]
    # (depth, slug) — parents (shallower) sort before children, which the
    # loader relies on, and the tie-break on the unique slug is deterministic.
    # Depth is derived from the exported parent_slug edges (not the treenode
    # tn_depth cache) so it is consistent with what the fixture actually says.
    depth_by_slug = _depths_by_slug(category_records)
    category_records.sort(key=lambda r: (depth_by_slug[r["slug"]], r["slug"]))

    # --- Sidecar state ------------------------------------------------------
    max_revision = max(
        Category.get_max_revision(), Feature.get_max_revision()
    )
    state = {
        "version": STATE_VERSION,
        "max_revision": max_revision,
        "features": {r["slug"]: content_hash(r) for r in feature_records},
        "categories": {r["slug"]: content_hash(r) for r in category_records},
    }
    return feature_records, category_records, state


def _depths_by_slug(records) -> dict:
    """Map each category slug to its depth by walking ``parent_slug`` edges.

    A parent that is itself filtered out of the export (test/deleted) is not
    in the record set, so the walk stops there — the orphaned child is treated
    as a root for sort purposes. The ``seen`` guard makes a cyclic edge (which
    the tree constraints forbid, but be defensive) terminate.
    """
    parent_of = {r["slug"]: r["parent_slug"] for r in records}
    depths: dict = {}

    def depth(slug: str) -> int:
        if slug in depths:
            return depths[slug]
        d = 0
        cur = parent_of.get(slug)
        seen = {slug}
        while cur is not None and cur in parent_of and cur not in seen:
            seen.add(cur)
            d += 1
            cur = parent_of.get(cur)
        depths[slug] = d
        return d

    for r in records:
        depth(r["slug"])
    return depths
