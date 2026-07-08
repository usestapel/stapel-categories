"""Catalog fixture reconciliation — load side (CAT-2).

The mirror of :mod:`stapel_categories.catalog_fixtures` (CAT-1's export). Reads
the natural-key JSON fixtures (``features.json`` / ``categories.json``) plus the
``.sync-state.json`` sidecar and reconciles them **into** the live
``Category`` / ``Feature`` / ``CategoryFeature`` tables.

Design: ``docs/catalog-fixtures-sync.md`` (§3–§4, §6). Key decisions realized
here:

* **3-way diff, not "fixture always wins".** For every natural key we compare
  three content-hashes — ``base`` (the sidecar, i.e. the last synced state),
  ``fixture`` (the file) and ``db`` (the live row, hashed exactly as export
  would serialize it). Only one side moving is a fast-forward; both sides
  moving is a conflict. The classification table is §4.
* **Writes go only through ``Model.save()`` / ``full_clean()``** — never
  ``bulk_create`` / ``QuerySet.update()`` (H-2 lesson). The loader must earn the
  same side effects as an admin/Studio edit: revision bump,
  ``category.changed`` fanout, ``copy_parent_features`` on a new child,
  config/slug validation.
* **Idempotent.** A record whose fixture state already equals its DB state is a
  ``skip`` — no ``.save()``, no revision bump, no event (the H-3 "don't bump on
  a non-change" rule). A second ``load_catalog`` on materialized fixtures is a
  no-op.
* **``is_test`` rows are invisible.** The DB view is built with
  ``build_catalog(include_test=False)``, so test rows never enter the diff:
  never created, updated, deleted or conflicted. If a fixture slug collides with
  a live ``is_test`` row the loader refuses to overwrite it (a per-record
  error), it does not silently clobber.
* **Subtree lock.** The whole reconciliation runs in one transaction that first
  ``select_for_update``-locks every existing ``Category``/``Feature`` row in
  deterministic pk order (the M-5 anti-deadlock pattern) so a concurrent
  admin/Studio edit serializes against the load instead of interleaving.

The sidecar is updated after a successful load to reflect the *applied* state:
reconciled keys advance to their new DB hash, deleted keys drop out, and keys we
deliberately did **not** touch (DB-only drift, unresolved conflicts) keep their
old base hash so they stay flagged on the next run — never a silent resolution.
"""
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from django.core.exceptions import ValidationError
from django.db import transaction

from . import catalog_fixtures as cf

# --- conflict / deletion policies ------------------------------------------
ON_CONFLICT_ABORT = "abort"
ON_CONFLICT_FIXTURE = "fixture-wins"
ON_CONFLICT_DB = "db-wins"
ON_CONFLICT_CHOICES = (ON_CONFLICT_ABORT, ON_CONFLICT_FIXTURE, ON_CONFLICT_DB)

DELETIONS_SOFT = "soft"
DELETIONS_HARD = "hard"
DELETIONS_IGNORE = "ignore"
DELETIONS_CHOICES = (DELETIONS_SOFT, DELETIONS_HARD, DELETIONS_IGNORE)

# --- report record kinds ----------------------------------------------------
CREATED = "created"
UPDATED = "updated"
SKIPPED = "skipped"
CONFLICT = "conflict"
DELETED = "deleted"
DB_ONLY = "db_only"      # changed in DB since last export — not touched, warn
DB_NEW = "db_new"        # present only in DB, never exported — "not in canon"
ERROR = "error"          # bad fixture record (validation / dangling reference)

# Inline (override) feature-list entries carry at least these keys; a bare
# reference is just ``{"slug": ...}``.
_INLINE_KEYS = ("config", "mandatory", "show_as_badge", "show_at_title", "translate")


class RecordError(Exception):
    """A single fixture record could not be applied (bad data / dangling ref).

    Isolated per record: the offending record is reported and skipped (exit
    code becomes non-zero), the rest of the load proceeds.
    """


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class Item:
    kind: str          # one of the record-kind constants
    key: str           # natural key (slug)
    detail: str = ""


@dataclass
class Report:
    dry_run: bool = False
    features: List[Item] = field(default_factory=list)
    categories: List[Item] = field(default_factory=list)

    def add(self, side: str, item: Item) -> None:
        (self.features if side == "features" else self.categories).append(item)

    def _all(self) -> List[Item]:
        return self.features + self.categories

    def count(self, kind: str) -> int:
        return sum(1 for it in self._all() if it.kind == kind)

    @property
    def conflicts(self) -> int:
        return self.count(CONFLICT)

    @property
    def errors(self) -> int:
        return self.count(ERROR)

    @property
    def failed(self) -> bool:
        """A load "failed" (non-zero exit) if any conflict or bad record."""
        return self.conflicts > 0 or self.errors > 0


# ---------------------------------------------------------------------------
# 3-way classification (§4)
# ---------------------------------------------------------------------------
#
# Raw classes, independent of policy:
_SKIP = "skip"                     # fixture == db == base
_CONVERGED = "converged"           # fixture == db, both != base (agree, advance base)
_CREATE = "create"                 # fixture only, no history
_FAST_FORWARD = "fast_forward"     # fixture changed, db unchanged
_DB_ONLY = "db_only"               # db changed, fixture unchanged
_DB_ONLY_DELETION = "db_only_del"  # fixture unchanged, db lost the row
_CONFLICT = "conflict"             # both sides diverged
_DELETE = "delete"                 # removed from fixture, db unchanged
_DELETE_CONFLICT = "delete_conf"   # removed from fixture, db changed locally
_DB_NEW = "db_new"                 # db only, never in canon
_GONE = "gone"                     # base only — dropped from both sides


def _classify(base: Optional[str], fixture: Optional[str], db: Optional[str]) -> str:
    fp, dp, bp = fixture is not None, db is not None, base is not None
    if fp and dp:
        if fixture == db:
            return _SKIP if (bp and base == fixture) else _CONVERGED
        if bp and db == base:
            return _FAST_FORWARD
        if bp and fixture == base:
            return _DB_ONLY
        return _CONFLICT
    if fp and not dp:
        if not bp:
            return _CREATE
        return _DB_ONLY_DELETION if fixture == base else _CONFLICT
    if dp and not fp:
        if not bp:
            return _DB_NEW
        return _DELETE if db == base else _DELETE_CONFLICT
    return _GONE


@dataclass
class Decision:
    """What to do with one natural key, after applying the CLI policies."""
    op: str            # 'upsert' | 'delete' | 'skip' | 'warn' | 'note' | 'touch_base' | 'drop_base'
    kind: str          # report kind
    reconciled: bool = False   # advance base to the applied DB hash
    removed: bool = False      # drop the key from base
    conflict: bool = False     # counts toward non-zero exit


def _decide(raw: str, *, db_present: bool, on_conflict: str, deletions: str) -> Decision:
    upsert_kind = UPDATED if db_present else CREATED

    if raw == _SKIP:
        return Decision("skip", SKIPPED)
    if raw == _CONVERGED:
        # Both sides reached the same value independently — no DB write needed,
        # just advance the base so it is no longer seen as diverged.
        return Decision("touch_base", SKIPPED, reconciled=True)
    if raw == _CREATE:
        return Decision("upsert", CREATED, reconciled=True)
    if raw == _FAST_FORWARD:
        return Decision("upsert", UPDATED, reconciled=True)
    if raw == _DB_NEW:
        return Decision("note", DB_NEW)
    if raw == _GONE:
        return Decision("drop_base", SKIPPED, removed=True)

    if raw == _DB_ONLY:
        if on_conflict == ON_CONFLICT_DB:
            return Decision("skip", SKIPPED)   # db-wins: silent, same effect
        return Decision("warn", DB_ONLY)       # default: keep DB, warn, base unchanged
    if raw == _DB_ONLY_DELETION:
        if on_conflict == ON_CONFLICT_FIXTURE:
            return Decision("upsert", CREATED, reconciled=True)  # resurrect from canon
        return Decision("warn", DB_ONLY)

    if raw == _DELETE:
        if deletions == DELETIONS_IGNORE:
            return Decision("skip", SKIPPED)
        return Decision("delete", DELETED, removed=True)

    if raw == _CONFLICT:
        if on_conflict == ON_CONFLICT_FIXTURE:
            return Decision("upsert", upsert_kind, reconciled=True)
        if on_conflict == ON_CONFLICT_DB:
            # Keep DB, discard the fixture change. Base is left at the old value
            # (they still disagree) so the next run does NOT silently fast-forward
            # the fixture over the DB — the divergence stays visible until an
            # export reconciles it.
            return Decision("skip", SKIPPED)
        return Decision("skip", CONFLICT, conflict=True)   # abort this record

    if raw == _DELETE_CONFLICT:
        if on_conflict == ON_CONFLICT_FIXTURE:
            if deletions == DELETIONS_IGNORE:
                return Decision("skip", SKIPPED)
            return Decision("delete", DELETED, removed=True)
        if on_conflict == ON_CONFLICT_DB:
            return Decision("skip", SKIPPED)
        return Decision("skip", CONFLICT, conflict=True)

    raise AssertionError(f"unhandled class {raw!r}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Fixture / sidecar IO
# ---------------------------------------------------------------------------


def _read_json(path: str, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _normalize_feature_record(rec: dict) -> dict:
    """Coerce a fixture feature record to the exact shape export writes.

    The 3-way diff compares content-hashes, and the DB side is hashed via
    ``build_catalog`` (the export serializer). A hand-written fixture that
    omits defaulted keys would hash-differ from its own applied DB state
    forever — every load would "fast-forward" it again, bumping revisions
    (an idempotency hole). Normalizing the fixture record to the export shape
    before hashing closes it. Defaults here MUST mirror the model field
    defaults / the ``_apply_*_upsert`` setters.
    """
    out = {
        "slug": rec["slug"],
        "name": rec.get("name", ""),
        "icon": rec.get("icon", ""),
        "comment": rec.get("comment", ""),
        "config": rec.get("config") or {},
        "mandatory": bool(rec.get("mandatory", False)),
        "show_as_badge": bool(rec.get("show_as_badge", False)),
        "show_at_title": bool(rec.get("show_at_title", False)),
        "translate": rec.get("translate", "all"),
    }
    if rec.get("is_test"):
        out["is_test"] = True
    return out


def _normalize_entry(entry: dict) -> dict:
    """Normalize one feature-list entry (bare reference or inline override)."""
    slug = entry.get("slug") or ""
    if not _is_inline(entry) and slug:
        return {"slug": slug}
    out = {
        "slug": slug,
        "config": entry.get("config") or {},
        "mandatory": bool(entry.get("mandatory", False)),
        "show_as_badge": bool(entry.get("show_as_badge", False)),
        "show_at_title": bool(entry.get("show_at_title", False)),
        "translate": entry.get("translate", "all"),
    }
    if not slug:
        # Slug-less rows carry their identity inline (no features.json home).
        out["name"] = entry.get("name", "")
        out["icon"] = entry.get("icon", "")
        out["comment"] = entry.get("comment", "")
    if entry.get("is_test"):
        out["is_test"] = True
    return out


def _normalize_category_record(rec: dict) -> dict:
    """Coerce a fixture category record to the exact shape export writes."""
    out = {
        "slug": rec["slug"],
        "parent_slug": rec.get("parent_slug"),
        "name": rec.get("name", ""),
        "comment": rec.get("comment", ""),
        "catalog_icon": rec.get("catalog_icon", ""),
        "carousel_icon": rec.get("carousel_icon", ""),
        "carousel_enabled": bool(rec.get("carousel_enabled", False)),
        "active": bool(rec.get("active", True)),
        "translatable": bool(rec.get("translatable", True)),
        "features": [_normalize_entry(e) for e in rec.get("features", [])],
    }
    if rec.get("is_test"):
        out["is_test"] = True
    return out


def _index_records(records: list, normalize, what: str) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for rec in records:
        slug = rec.get("slug") if isinstance(rec, dict) else None
        if not slug:
            raise ValueError(f"{what}: record without a 'slug' natural key: {rec!r}")
        if slug in out:
            raise ValueError(f"{what}: duplicate natural key '{slug}'")
        out[slug] = normalize(rec)
    return out


def _load_inputs(directory: str):
    """Read the two fixtures + the sidecar. Returns (fix_feat, fix_cat, base).

    Records are normalized to the canonical export shape (see
    :func:`_normalize_feature_record`) so hashing, planning and applying all
    work on one consistent form.
    """
    features = _read_json(os.path.join(directory, cf.FEATURES_FILE), [])
    categories = _read_json(os.path.join(directory, cf.CATEGORIES_FILE), [])
    base = _read_json(os.path.join(directory, cf.STATE_FILE), None)
    fix_feat = _index_records(features, _normalize_feature_record, cf.FEATURES_FILE)
    fix_cat = _index_records(categories, _normalize_category_record, cf.CATEGORIES_FILE)
    return fix_feat, fix_cat, base


# ---------------------------------------------------------------------------
# Apply helpers — always through .save()/.full_clean()
# ---------------------------------------------------------------------------


def _save_feature(feat) -> None:
    """Validate (slug-bearing rows only) and save.

    Slug-less features (``header`` display rows) intentionally skip
    ``full_clean`` — the model's ``clean`` requires a slug on a root, and these
    rows are created bare exactly as the feature editor's ``create`` path does.
    """
    if feat.slug:
        feat.full_clean()
    feat.save()


# Scalar fields the loader owns on each model — used by the dirty guards below
# so an upsert whose target state already equals the DB state never save()s
# (no phantom revision bump / category.changed emit; the H-3 rule holds even
# for records the 3-way hash classified as changed but whose *applicable*
# state is unchanged — e.g. hand-written fixtures with unreachable parts).
_FEATURE_SCALARS = (
    "slug", "name", "icon", "comment", "config", "mandatory",
    "show_as_badge", "show_at_title", "translate", "is_test", "deleted",
)
_CATEGORY_SCALARS = (
    "slug", "name", "comment", "catalog_icon", "carousel_icon",
    "carousel_enabled", "active", "translatable", "is_test", "deleted",
    "tn_parent_id",
)


def _snapshot(obj, fields) -> dict:
    return {f: getattr(obj, f) for f in fields}


def _apply_feature_upsert(record: dict):
    from .models import Feature

    slug = record["slug"]
    existing = Feature.objects.filter(slug=slug, tn_parent__isnull=True).first()
    if existing is not None and existing.is_test:
        raise RecordError(
            f"root feature slug '{slug}' is occupied by an is_test row — not overwriting"
        )
    feat = existing or Feature(tn_parent=None)
    before = _snapshot(feat, _FEATURE_SCALARS) if existing is not None else None
    feat.slug = slug
    feat.name = record.get("name", "")
    feat.icon = record.get("icon", "")
    feat.comment = record.get("comment", "")
    feat.config = record.get("config") or {}
    feat.mandatory = bool(record.get("mandatory", False))
    feat.show_as_badge = bool(record.get("show_as_badge", False))
    feat.show_at_title = bool(record.get("show_at_title", False))
    feat.translate = record.get("translate", "all")
    feat.is_test = bool(record.get("is_test", False))
    feat.deleted = False  # restore if it had been soft-deleted
    if before is not None and before == _snapshot(feat, _FEATURE_SCALARS):
        return feat  # dirty guard: nothing to write, no bump, no emit
    _save_feature(feat)
    return feat


def _root_feature(slug: str, referencing: str):
    from .models import Feature

    feat = Feature.objects.filter(
        slug=slug, tn_parent__isnull=True, deleted=False
    ).first()
    if feat is None:
        raise RecordError(
            f"category '{referencing}' references feature slug '{slug}' "
            "with no root definition in features.json"
        )
    if feat.is_test:
        raise RecordError(
            f"category '{referencing}' references is_test feature '{slug}'"
        )
    return feat


def _is_inline(entry: dict) -> bool:
    return any(k in entry for k in _INLINE_KEYS)


def _entry_matches(feat, desired: dict) -> bool:
    return all(
        v is None or getattr(feat, k) == v for k, v in desired.items()
    )


def _materialize_override(cat, slug: str, entry: dict, used: set):
    """Find-or-create-or-update the per-category inline row for ``entry``.

    An inline entry is either an override (its own ``config``/flags hanging off
    the shared root, ``tn_parent`` set) or a slug-less display row (e.g.
    ``header``). Resolution order, for idempotency:

    1. Reuse a row already linked to *this* category for this slug whose state
       already matches the entry — zero writes on a re-load.
    2. Else reuse such a row and edit it in place (the editor's ``edit``
       semantics) — but **only if no other category links it**. A row shared
       with other categories (``inherit``-propagation / ``copy_parent_features``
       copies) is copied-on-write instead: mutating it in place would silently
       rewrite every sibling's schema, which this category's fixture record has
       no authority over.
    3. Else create a fresh child row under the root (or a fresh slug-less row).

    ``used`` holds feature pks already claimed by earlier entries of this
    category's list (two slug-less header rows must map to two distinct rows).
    Returns ``(feature, changed)``.
    """
    from .models import Feature

    root = _root_feature(slug, cat.slug) if slug else None

    desired = {
        "name": entry.get("name", "") if not slug else None,
        "icon": entry.get("icon", "") if not slug else None,
        "comment": entry.get("comment", "") if not slug else None,
        "config": entry.get("config") or {},
        "mandatory": bool(entry.get("mandatory", False)),
        "show_as_badge": bool(entry.get("show_as_badge", False)),
        "show_at_title": bool(entry.get("show_at_title", False)),
        "translate": entry.get("translate", "all"),
    }

    # Candidate rows already linked to this category that export would render
    # inline for this slug: overrides (tn_parent set) or slug-less rows.
    candidates = []
    for link in cat.category_features.select_related("feature").order_by("order", "id"):
        f = link.feature
        if f is None or f.pk in used or f.deleted:
            continue
        if (f.slug or "") != slug:
            continue
        if f.tn_parent_id is not None or not slug:
            candidates.append(f)

    feat = next((f for f in candidates if _entry_matches(f, desired)), None)
    if feat is not None:
        used.add(feat.pk)
        return feat, False  # already exactly the fixture state

    if candidates:
        feat = candidates[0]
        shared = feat.feature_categories.exclude(category=cat).exists()
        if not shared:
            for k, v in desired.items():
                if v is not None and getattr(feat, k) != v:
                    setattr(feat, k, v)
            _save_feature(feat)
            used.add(feat.pk)
            return feat, True
        # fall through: shared row -> copy-on-write below

    feat = Feature(tn_parent=root, slug=slug)
    if slug:
        # A slug-bearing override keeps the root's identity fields.
        feat.name = root.name
        feat.icon = root.icon
        feat.comment = root.comment
    for k, v in desired.items():
        if v is not None:
            setattr(feat, k, v)
    feat.is_test = bool(entry.get("is_test", False))
    _save_feature(feat)
    used.add(feat.pk)
    return feat, True


def _rewrite_orders(cat, ordered_features) -> bool:
    """Make ``cat``'s CategoryFeature rows exactly ``ordered_features``.

    Mirrors ``feature_editor._rewrite_orders``: create missing links, fix
    orders, delete stale ones — each guarded so an unchanged list is zero
    writes. Returns whether anything changed.
    """
    from .models import CategoryFeature

    existing = {link.feature_id: link for link in cat.category_features.all()}
    changed = False
    seen = set()
    for idx, feat in enumerate(ordered_features):
        if feat.pk in seen:
            continue  # defensive: a slug referenced twice
        seen.add(feat.pk)
        link = existing.get(feat.pk)
        if link is not None:
            if link.order != idx:
                link.order = idx
                link.save(update_fields=["order"])
                changed = True
        else:
            CategoryFeature.objects.create(category=cat, feature=feat, order=idx)
            changed = True
    stale = [fid for fid in existing if fid not in seen]
    if stale:
        cat.category_features.filter(feature_id__in=stale).delete()
        _cleanup_orphaned_overrides(stale)
        changed = True
    return changed


def _cleanup_orphaned_overrides(feature_ids) -> None:
    """Soft-delete override rows a stale-link removal just made unreachable.

    An override (``tn_parent`` set) exists only to be linked from one or more
    categories' materialized list — unlike a root feature, it has no home in
    ``features.json``. If the link just removed above was its last one, the
    row is now reachable from nowhere: invisible to every future export (dropped
    silently by ``_category_record``'s per-category walk) and to every future
    load (no fixture ever references it), so it would sit in the table forever
    — a leak that accumulates one row per removed override across repeated
    fixture edits (the fixtures-sync review's "orphan override" finding).
    Root features (``tn_parent`` NULL) are never touched here: they are
    addressed by ``features.json``, independent of any one category's list.
    """
    from .models import Feature

    orphans = Feature.objects.filter(
        pk__in=feature_ids, tn_parent__isnull=False, deleted=False,
    )
    for feat in orphans:
        if not feat.feature_categories.exists():
            feat.soft_delete()


def _reconcile_features(cat, entries: list) -> bool:
    """Bring ``cat``'s materialized feature list to match ``entries``."""
    target = []
    changed = False
    used: set = set()
    seen_pks: set = set()
    for entry in entries:
        slug = entry.get("slug") or ""
        if _is_inline(entry):
            feat, feat_changed = _materialize_override(cat, slug, entry, used)
            changed = changed or feat_changed
        else:
            feat = _root_feature(slug, cat.slug)
        if feat.pk in seen_pks:
            # Two entries resolving to one row (e.g. a bare reference listed
            # twice) can never be materialized: the applied list would hash-
            # differ from the fixture forever, so every future load would
            # re-"apply" it. Refuse loudly instead of churning silently.
            raise RecordError(
                f"category '{cat.slug}': duplicate feature entry "
                f"'{slug or '(slug-less)'}' — two list entries resolve to one row"
            )
        seen_pks.add(feat.pk)
        target.append(feat)
    if _rewrite_orders(cat, target):
        changed = True
    return changed


def _apply_category_upsert(record: dict):
    from .models import Category

    slug = record["slug"]
    existing = Category.objects.filter(slug=slug).first()
    if existing is not None and existing.is_test:
        raise RecordError(
            f"category slug '{slug}' is occupied by an is_test row — not overwriting"
        )
    created = existing is None
    cat = existing or Category()
    before = _snapshot(cat, _CATEGORY_SCALARS) if existing is not None else None

    parent_slug = record.get("parent_slug")
    if parent_slug == slug:
        raise RecordError(f"category '{slug}' references itself as parent_slug")
    parent = None
    if parent_slug:
        parent = Category.objects.filter(slug=parent_slug, deleted=False).first()
        if parent is None:
            raise RecordError(
                f"category '{slug}' references unknown parent_slug '{parent_slug}'"
            )

    cat.slug = slug
    cat.name = record.get("name", "")
    cat.comment = record.get("comment", "")
    cat.catalog_icon = record.get("catalog_icon", "")
    cat.carousel_icon = record.get("carousel_icon", "")
    cat.carousel_enabled = bool(record.get("carousel_enabled", False))
    cat.active = bool(record.get("active", True))
    cat.translatable = bool(record.get("translatable", True))
    cat.is_test = bool(record.get("is_test", False))
    cat.deleted = False
    cat.tn_parent = parent

    if created:
        # No pk yet → Category.clean() skips validate_features; first save
        # assigns the pk and fires copy_parent_features (parent links copied).
        cat.full_clean()
        cat.save()
        features_changed = _reconcile_features(cat, record.get("features", []))
        if features_changed:
            cat.full_clean()  # pk set now → validate the final feature set
            cat.save()        # bump/emit reflecting the reconciled schema
    else:
        features_changed = _reconcile_features(cat, record.get("features", []))
        if features_changed or before != _snapshot(cat, _CATEGORY_SCALARS):
            cat.full_clean()  # validate_features over the reconciled set
            cat.save()        # single bump/emit for scalar + feature changes
        # else: dirty guard — the hash diff flagged this record, but nothing
        # applicable actually differs (e.g. an unreachable hand-written part
        # such as an is_test inline entry, which the export view excludes).
        # Saving anyway would bump revision + emit on EVERY load (H-3 rule).
    return cat, created


def _feature_tree_pks(root) -> list:
    """Pks of a root feature and every override row hanging under it (BFS)."""
    from .models import Feature

    pks = [root.pk]
    frontier = [root.pk]
    while frontier:
        frontier = list(
            Feature.objects.filter(tn_parent_id__in=frontier).values_list("pk", flat=True)
        )
        pks.extend(frontier)
    return pks


def _apply_feature_delete(slug: str, deletions: str) -> bool:
    from .models import CategoryFeature, Feature

    feat = Feature.objects.filter(slug=slug, tn_parent__isnull=True, deleted=False).first()
    if feat is None or feat.is_test:
        return False
    if deletions == DELETIONS_SOFT:
        feat.soft_delete()
    elif deletions == DELETIONS_HARD:
        # A hard delete CASCADEs to every override child row and every
        # CategoryFeature link (treenode tn_parent + the FK are CASCADE) — it
        # would silently strip the feature from any category still carrying
        # it. A consistent fixture unlinks in the upsert phase (which runs
        # first); remaining links mean the fixture never asked for this.
        links = CategoryFeature.objects.filter(
            feature_id__in=_feature_tree_pks(feat), category__deleted=False
        )
        if links.exists():
            raise RecordError(
                f"refusing hard delete of feature '{slug}': it (or an override "
                "of it) is still linked by a live category — remove the "
                "entries from those categories' fixture records first"
            )
        feat.delete()
    return True


def _apply_category_delete(slug: str, deletions: str) -> bool:
    from .models import Category

    cat = Category.objects.filter(slug=slug, deleted=False).first()
    if cat is None or cat.is_test:
        return False
    if deletions == DELETIONS_SOFT:
        cat.soft_delete()
    elif deletions == DELETIONS_HARD:
        # treenode's delete() cascades the whole subtree (tn_parent CASCADE,
        # inside no_signals) — hard-deleting a parent would silently take down
        # live children the fixture still declares (or is_test scratch rows,
        # which must be invisible to the loader). Deletes are ordered
        # children-first (see _run_plan), so a whole-subtree removal still
        # works: by the time the parent is processed its children are gone.
        if cat.tn_children.filter(deleted=False).exists():
            raise RecordError(
                f"refusing hard delete of category '{slug}': it still has live "
                "children (treenode cascade would silently delete them)"
            )
        cat.delete()
    return True


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass
class _Planned:
    key: str
    decision: Decision
    record: Optional[dict] = None   # fixture record for upserts


def _plan_side(fix: dict, base: dict, db_hashes: dict, *, on_conflict, deletions):
    """Classify every natural key on one side (features or categories)."""
    keys = set(fix) | set(base) | set(db_hashes)
    planned: List[_Planned] = []
    for key in sorted(keys):
        f_hash = cf.content_hash(fix[key]) if key in fix else None
        b_hash = base.get(key)
        d_hash = db_hashes.get(key)
        raw = _classify(b_hash, f_hash, d_hash)
        decision = _decide(
            raw,
            db_present=d_hash is not None,
            on_conflict=on_conflict,
            deletions=deletions,
        )
        planned.append(_Planned(key, decision, fix.get(key)))
    return planned


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def load_catalog(
    directory: str,
    *,
    dry_run: bool = False,
    on_conflict: str = ON_CONFLICT_ABORT,
    deletions: str = DELETIONS_SOFT,
    seed_if_empty: bool = False,
):
    """Reconcile the fixtures in ``directory`` into the live catalog.

    Returns a :class:`Report`. Writes the updated ``.sync-state.json`` sidecar
    on a real (non ``dry_run``) run. Raises nothing for conflicts — the caller
    inspects ``report.failed`` for the exit code.
    """
    from .models import Category, Feature

    fix_feat, fix_cat, base = _load_inputs(directory)
    if base is not None and base.get("version") != cf.STATE_VERSION:
        raise ValueError(
            f"incompatible .sync-state.json version {base.get('version')!r} "
            f"(expected {cf.STATE_VERSION}); regenerate via export_catalog"
        )
    base_feat = (base or {}).get("features", {})
    base_cat = (base or {}).get("categories", {})

    report = Report(dry_run=dry_run)

    # --- seed-if-empty short-circuit (load_staff_group_if_empty idiom) -------
    if seed_if_empty:
        # is_test rows are outside canon by construction (§5) — a DB that
        # holds only test/scratch data must still read as "empty" here, or a
        # test suite that seeds is_test fixtures before calling
        # --seed-if-empty would silently strand the canon out forever (the
        # bootstrap idiom's whole point is a guaranteed first load).
        db_empty = (
            not Category.objects.filter(is_test=False).exists()
            and not Feature.objects.filter(is_test=False).exists()
        )
        if not db_empty:
            report.categories.append(Item(
                SKIPPED, "*",
                "catalog is not empty — --seed-if-empty is a no-op "
                "(use load_catalog without it to sync)",
            ))
            return report
        # Empty DB: ignore the sidecar base entirely so every fixture record is a
        # clean create (a populated sidecar shipped in the repo must not turn a
        # fresh bootstrap into a wall of "db deleted it" warnings).
        base_feat, base_cat = {}, {}

    if dry_run:
        _run_plan(
            report, fix_feat, fix_cat, base_feat, base_cat,
            on_conflict=on_conflict, deletions=deletions, apply=False,
        )
        return report

    new_state = None
    with transaction.atomic():
        # Subtree lock (M-5): serialize the whole load against concurrent
        # admin/Studio edits. Deterministic pk order avoids deadlocks; a no-op
        # on backends without row locking (the revision mutex still serializes
        # saves there).
        list(Feature.objects.select_for_update().order_by("pk").values_list("pk", flat=True))
        list(Category.objects.select_for_update().order_by("pk").values_list("pk", flat=True))

        new_state = _run_plan(
            report, fix_feat, fix_cat, base_feat, base_cat,
            on_conflict=on_conflict, deletions=deletions, apply=True,
        )

    # The sidecar reflects the applied state — written after commit.
    with open(os.path.join(directory, cf.STATE_FILE), "w", encoding="utf-8") as fh:
        fh.write(cf.canonical_json(new_state))
    return report


def _run_plan(
    report, fix_feat, fix_cat, base_feat, base_cat, *, on_conflict, deletions, apply
):
    """Classify and (optionally) apply, in referential order.

    Order: feature upserts → category upserts (parents first) → category
    deletes → feature deletes. Deletes come last so a row is never removed
    while something still references it. Returns the new sidecar state (or
    ``None`` for a dry run).
    """
    # DB view (excludes is_test + soft-deleted, exactly like export).
    _, _, db_state = cf.build_catalog(include_test=False)
    db_feat = db_state["features"]
    db_cat = db_state["categories"]

    feat_plan = _plan_side(
        fix_feat, base_feat, db_feat, on_conflict=on_conflict, deletions=deletions
    )
    cat_plan = _plan_side(
        fix_cat, base_cat, db_cat, on_conflict=on_conflict, deletions=deletions
    )

    # Category upserts must run parents-before-children (a child's create needs
    # its parent to exist and to have its features already reconciled so
    # copy_parent_features copies the right rows).
    cat_depth = cf._depths_by_slug(list(fix_cat.values()))
    cat_upserts = sorted(
        (p for p in cat_plan if p.decision.op == "upsert"),
        key=lambda p: (cat_depth.get(p.key, 0), p.key),
    )

    # Category deletes run children-first (deepest first, by the LIVE tree):
    # with --deletions hard the parent's guard requires its children to be
    # gone already, so a whole-subtree removal deletes leaves upward.
    db_depths = _db_category_depths()
    cat_deletes = sorted(
        (p for p in cat_plan if p.decision.op == "delete"),
        key=lambda p: (-db_depths.get(p.key, 0), p.key),
    )

    if apply:
        _apply_phase(report, "features", [p for p in feat_plan if p.decision.op == "upsert"],
                     _apply_feature_upsert)
        _apply_phase(report, "categories", cat_upserts,
                     _apply_category_upsert)
        _apply_delete_phase(report, "categories", cat_deletes,
                            _apply_category_delete, deletions)
        _apply_delete_phase(report, "features",
                            [p for p in feat_plan if p.decision.op == "delete"],
                            _apply_feature_delete, deletions)
        # Non-mutating outcomes (skip/warn/note/touch_base/drop_base).
        _record_passive(report, "features", feat_plan)
        _record_passive(report, "categories", cat_plan)

        # Sidecar reflects the applied state. Deliberately NO "max_revision":
        # that key is export's pre-filter base ("has the DB changed since the
        # last EXPORT"). If a load wrote the post-load max here, the very next
        # export_catalog — including the one the db-only-drift warning tells
        # the operator to run — would see an unmoved max(revision) and silently
        # skip, stranding the drift out of canon forever.
        _, _, db_after = cf.build_catalog(include_test=False)
        return {
            "version": cf.STATE_VERSION,
            "features": _new_base(base_feat, feat_plan, db_after["features"], report, "features"),
            "categories": _new_base(base_cat, cat_plan, db_after["categories"], report, "categories"),
        }

    # dry run: just report intended outcomes
    for side, plan in (("features", feat_plan), ("categories", cat_plan)):
        for p in plan:
            report.add(side, Item(p.decision.kind, p.key, _detail(p)))
    return None


def _db_category_depths() -> dict:
    """Depth of every live category slug, walking the DB ``tn_parent`` edges."""
    from .models import Category

    rows = list(Category.objects.values_list("pk", "slug", "tn_parent_id"))
    parent_of = {pk: parent for pk, _, parent in rows}
    depths: Dict[str, int] = {}
    for pk, slug, parent in rows:
        d, cur, seen = 0, parent, {pk}
        while cur is not None and cur not in seen:
            seen.add(cur)
            d += 1
            cur = parent_of.get(cur)
        depths[slug] = d
    return depths


def _apply_phase(report, side, planned, apply_fn):
    for p in planned:
        try:
            with transaction.atomic():  # savepoint: isolate a bad record
                apply_fn(p.record)
            report.add(side, Item(p.decision.kind, p.key, _detail(p)))
        except (RecordError, ValidationError) as exc:
            # Keep the original decision op ("upsert") so _record_passive does
            # not double-report; _new_base skips errored keys via the report.
            report.add(side, Item(ERROR, p.key, _fmt_exc(exc)))


def _apply_delete_phase(report, side, planned, apply_fn, deletions):
    for p in planned:
        try:
            with transaction.atomic():
                apply_fn(p.key, deletions)
            report.add(side, Item(DELETED, p.key, _detail(p)))
        except (RecordError, ValidationError) as exc:
            report.add(side, Item(ERROR, p.key, _fmt_exc(exc)))


def _record_passive(report, side, plan):
    for p in plan:
        if p.decision.op in ("skip", "warn", "note", "touch_base", "drop_base"):
            report.add(side, Item(p.decision.kind, p.key, _detail(p)))


def _new_base(old_base, plan, db_after_hashes, report, side):
    """Build the new sidecar hashes for one side from the applied outcomes."""
    result = dict(old_base)
    # Records that errored were re-tagged with kind ERROR; find them so their
    # base entry is left untouched.
    errored = {it.key for it in (report.features if side == "features" else report.categories)
               if it.kind == ERROR}
    for p in plan:
        if p.key in errored:
            continue
        dec = p.decision
        if dec.removed:
            result.pop(p.key, None)
        elif dec.reconciled:
            h = db_after_hashes.get(p.key)
            if h is not None:
                result[p.key] = h
            else:
                # e.g. a fixture that created an is_test row (excluded from the
                # DB view) — nothing to track.
                result.pop(p.key, None)
        # else: untouched (db-only drift, unresolved conflict, plain skip) —
        # keep the old base hash so it stays visible next run.
    return result


def _detail(p: _Planned) -> str:
    dec = p.decision
    if dec.kind == CONFLICT:
        return "diverged in both fixture and DB — run with --on-conflict to resolve"
    if dec.kind == DB_ONLY:
        return "changed in DB since last export — run export_catalog before load_catalog"
    if dec.kind == DB_NEW:
        return "present only in DB (not in canon)"
    return ""


def _fmt_exc(exc) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(f"{k}: {v}" for k, v in exc.message_dict.items()) \
            if hasattr(exc, "message_dict") else "; ".join(exc.messages)
    return str(exc)
