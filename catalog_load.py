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
* **Identity before slug.** A fixture row carrying ``external_id`` is matched
  against the live row with the same ``(external_source, external_id)`` before
  the slug is tried; only rows without an external id fall back to the slug.
  An imported slug is derived from the source's node path, so a source-side
  rename moves it — matching on it would read the rename as a delete plus an
  unrelated create and duplicate the node. See the section further down.
* **Idempotent, and it CONVERGES.** A record whose fixture state already
  equals its DB state is a ``skip`` — no ``.save()``, no revision bump, no
  event (the H-3 "don't bump on a non-change" rule). A second ``load_catalog``
  on materialized fixtures is a no-op. The stronger rule, since 0.20.2: a
  second load is a no-op *even where the applied row cannot hash equal to the
  fixture record*. The sidecar records a PAIR per key — the fixture hash that
  was applied and the DB hash it produced — and the diff asks which SIDE MOVED
  since that sync, not which two hashes look alike. Recording the DB hash
  alone made every such record re-plan ``updated`` on every pass forever (303
  of 3444 categories on one live catalogue, converging never), because the
  fixture side was then compared against a hash it could never equal. A record
  that lands not-equal is reported once as ``residual``, never silently.
* **A fixture never erases what it does not state (0.20.3).** An optional
  category scalar the record leaves out (``_OPTIONAL_CATEGORY_SCALARS``) keeps
  the live value; only an explicit key — ``""`` / ``auto`` included — clears a
  column, and an unsaid key is hashed as the value it keeps, so it produces no
  ``updated`` row. The export writes ``children_as`` / ``children_axis_label``
  / ``external_source`` only when set, so "absent" is a shape canon itself
  produces: applying it as ``""`` is how one reload blanked every axis caption
  ``derive_children_as --apply`` had just written. The load reports how many
  values it kept.
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
reconciled keys advance to the pair they were synced at, deleted keys drop out, and keys we
deliberately did **not** touch (DB-only drift, unresolved conflicts) keep their
old base hash so they stay flagged on the next run — never a silent resolution.
"""
import contextlib
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
# A db_new row whose PARENT the fixture owns. A deliberately local category
# is a local root or hangs under a local parent; a hand row parked between
# imported canon siblings is duplicate-shaped — on a live stand, seed
# children («Smartphones», «Laptops»…) sat beside the imported canon's own
# («Phones», «Notebooks»…) and sellers picked between near-duplicates while
# the report filed both states under the same generic db_new note.
DB_NEW_IN_CANON = "db_new_in_canon"
# Two LIVE, active, non-deleted siblings carrying the same case-folded name —
# what a seller experiences as one option offered twice (the stand's real
# case: two active «Другое» under one parent). Diagnosed over the whole live
# tree after apply (and over the current tree in a dry run), not per fixture
# record: either colliding row may be hand-seeded, imported, or years old.
NAME_COLLISION = "name_collision"
# Applied, and still not equal to the fixture: the row the write produced
# hashes differently from the record that asked for it, so an export would
# write something else. The sidecar records both sides (§4.1), so the record
# does NOT re-plan — which is exactly why this note exists.
RESIDUAL = "residual"
# A feature-slug rename this load DETECTED and did not perform. The slug is the
# key every listing files its answer under, so renaming it here without moving
# the stored answers strands them under a key the schema no longer knows — the
# 2026-09-05 incident this kind exists to make impossible to repeat silently.
# See the "Feature renames" section below.
RENAME_BLOCKED = "rename_blocked"
ERROR = "error"          # bad fixture record (validation / dangling reference)

# Inline (override) feature-list entries carry at least these keys; a bare
# reference is just ``{"slug": ...}``.
_INLINE_KEYS = (
    "config", "mandatory", "show_as_badge", "show_at_title", "visibility", "translate",
    "rules", "description", "example", "default", "hints", "group",
    # A per-category RENAME is an override on its own: an entry may carry the
    # root's config verbatim and differ only in the label it puts on the field.
    # Without `name` here such an entry read as a bare reference and the rename
    # was dropped on the way in, the mirror image of the export dropping it on
    # the way out.
    "name",
)


def _disclosure(source: dict) -> dict:
    """The reconciled ``{visibility, show_as_badge, show_at_title}`` triple.

    ``Feature.coerce_visibility`` silences both display flags on a non-public
    feature, so a fixture that asserts the contradiction ("hidden, and shown at
    title") would never equal the row it had just written — a phantom revision
    bump on every load. Reconciling here, by the same rule, keeps the 3-way
    diff idempotent. An unrecognized value is treated as non-public (fail
    closed) and then rejected by ``full_clean`` on the way in.
    """
    visibility = source.get("visibility") or "public"
    badge = bool(source.get("show_as_badge", False))
    title = bool(source.get("show_at_title", False))
    if visibility != "public":
        badge = title = False
    return {"show_as_badge": badge, "show_at_title": title, "visibility": visibility}


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
    #: This record moved slug because its SOURCE identity matched an existing
    #: row under a different slug — an update in place, not an add + remove.
    #: Carried on the item so the report can call it out separately.
    renamed: bool = False


@dataclass
class Report:
    dry_run: bool = False
    features: List[Item] = field(default_factory=list)
    categories: List[Item] = field(default_factory=list)
    #: Slugs of active leaf categories that type nothing, measured over the
    #: tree the load just produced (real runs only — a dry run has no "after").
    #: Not a failure: the load applied exactly what it was asked to; whether
    #: dead ends block a deploy is ``catalog_health``'s call (that command IS
    #: a gate). Carried here so the import that creates them says so at
    #: import time instead of at the next audit.
    dead_end_leaves: List[str] = field(default_factory=list)
    #: Slugs of active categories left hanging under an INACTIVE parent,
    #: measured over the same after-tree. Same standing: not a failure of the
    #: load, carried so the import that produces the shape says so at import
    #: time. A load can no longer CAUSE one (``active`` is create-only since
    #: 0.15.0), which is exactly why it is worth reporting — after this
    #: release, one here means the resurrection came from somewhere else.
    resurrected: List[str] = field(default_factory=list)
    #: ``{optional scalar: how many category records left it unsaid over a
    #: live, non-default value}`` — what this load KEPT rather than blanked
    #: (see :data:`_OPTIONAL_CATEGORY_SCALARS`). Reported because the erasure
    #: it replaces was silent: a reload answered ``children_axis_label: ''``
    #: for every derived chip row and said nothing about having done it.
    kept_unsaid: Dict[str, int] = field(default_factory=dict)
    #: ``{old feature slug: new}`` this load detected — whether or not it was
    #: allowed to perform them (``feature_renames_applied`` says which).
    feature_renames: Dict[str, str] = field(default_factory=dict)
    #: The same, per category slug: the map each hook call is made with.
    feature_renames_by_category: Dict[str, Dict[str, str]] = field(default_factory=dict)
    #: Whether the renames above were APPLIED (``--rename-features``) or kept
    #: at their live slugs and reported as blocked.
    feature_renames_applied: bool = False
    #: The comm Function the renames were handed to, "" when none was called.
    rename_hook: str = ""
    #: One entry per hook call: ``{category, renames, result|error}``.
    rename_hook_results: List[dict] = field(default_factory=list)

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

    @property
    def residuals(self) -> int:
        return self.count(RESIDUAL)

    @property
    def renames(self) -> int:
        return sum(1 for it in self._all() if it.renamed)


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


def _base_pair(entry):
    """The ``(fixture, db)`` hashes one sidecar entry records for a key.

    A sidecar entry is the pair the last successful sync produced: the FIXTURE
    hash that was applied and the DB hash that apply left behind. They are not
    always equal — the DB can hold a state the fixture shape cannot spell (see
    §4.1) — and storing only the DB half is what made such a record re-plan
    "updated" on every pass forever (0.20.1 and older: 303 of 3444 categories
    on one live catalogue).

    A LEGACY entry is a bare string — the applied DB hash, written before the
    pair existed. Read as both halves it classifies exactly as it did then, so
    an old sidecar keeps working and upgrades itself on the next load.
    """
    if entry is None:
        return None, None
    if isinstance(entry, str):
        return entry, entry
    return entry.get("fixture"), entry.get("db")


def _classify(base, fixture: Optional[str], db: Optional[str]) -> str:
    """Which side MOVED since the last sync — not which sides look alike.

    Both halves of the base are compared to their own side, so "the fixture is
    unchanged and nobody edited the DB" is a skip even where the two hashes
    differ, and a real DB-side edit still moves the DB half and conflicts.
    """
    base_f, base_d = _base_pair(base)
    fp, dp, bp = fixture is not None, db is not None, base is not None
    f_moved = not bp or fixture != base_f
    d_moved = not bp or db != base_d
    if fp and dp:
        if not f_moved and not d_moved:
            return _SKIP
        if f_moved and not d_moved:
            return _FAST_FORWARD
        if d_moved and not f_moved:
            return _DB_ONLY
        return _CONVERGED if fixture == db else _CONFLICT
    if fp and not dp:
        if not bp:
            return _CREATE
        return _CONFLICT if f_moved else _DB_ONLY_DELETION
    if dp and not fp:
        if not bp:
            return _DB_NEW
        return _DELETE_CONFLICT if d_moved else _DELETE
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
        if on_conflict == ON_CONFLICT_FIXTURE:
            # fixture-wins reverts a db-only EDIT too, not only a db-only
            # deletion (_DB_ONLY_DELETION below already resurrects from canon
            # under this policy). Leaving the two halves asymmetric made the
            # flag mean "the fixture wins, unless the DB got there first",
            # and it did not converge: a row the fixture never changes again
            # is classified db_only forever, so the drift is never revertible
            # by re-running the load. That is not academic for a FEATURE — a
            # root whose config.type drifted (two fixture directories sharing
            # one feature-slug namespace, the narrower one loaded last) makes
            # every per-category override the fixture carries for it
            # unwritable, and every category record holding one fails
            # validation on every pass.
            return Decision("upsert", UPDATED, reconciled=True)
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
        **_disclosure(rec),
        "translate": rec.get("translate", "all"),
        "rules": rec.get("rules") or [],
        "description": rec.get("description", ""),
        "example": rec.get("example", ""),
        "default": rec.get("default"),
        "hints": rec.get("hints") or [],
        "group": rec.get("group", ""),
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
        **_disclosure(entry),
        "translate": entry.get("translate", "all"),
        "rules": entry.get("rules") or [],
        "description": entry.get("description", ""),
        "example": entry.get("example", ""),
        "default": entry.get("default"),
        "hints": entry.get("hints") or [],
        "group": entry.get("group", ""),
    }
    if not slug:
        # Slug-less rows carry their identity inline (no features.json home).
        out["name"] = entry.get("name", "")
        out["icon"] = entry.get("icon", "")
        out["comment"] = entry.get("comment", "")
    elif "name" in entry:
        # A slug-bearing override that states its own label. Absent means
        # "inherit the root's", which is what every fixture written before this
        # said and what most entries still say — so normalization must not
        # invent an empty string here, or every such entry would read as a
        # rename to "".
        out["name"] = entry["name"]
    if entry.get("is_test"):
        out["is_test"] = True
    return out


# Category scalars a fixture record may leave UNSAID — and the whole point of
# the list: an absent key is not an instruction to blank the column.
#
# The export writes `children_as`, `children_axis_label` and
# `external_source` only when they are set, so absence is a shape CANON
# ITSELF produces. Reading it as "" is how a full reload
# (`--on-conflict fixture-wins`) blanked every axis caption
# `derive_children_as --apply` had just written: three of four chip rows came
# back answering `children_axis_label: ''`, and the fourth kept its label only
# because that one record happened to state it. The rest of the list are keys
# a hand-written record may omit for exactly the same reason — a fixture must
# not erase what it does not state.
#
# The rule: an ABSENT key keeps the live value; a STATED one is applied, the
# unset value (`""` / `auto`) included — that is how a fixture CLEARS a
# column. A row being created has nothing to keep and takes the field default.
#
# Not on the list, and why:
#   * `slug` / `parent_slug` / `name` / `features` — identity, structure and
#     content the export always states; absence there is a malformed record,
#     not a "keep". `parent_slug` could not use this rule anyway: an absent
#     key and an explicit `null` both mean "this is a root".
#   * `catalog_icon` / `carousel_icon` / `carousel_enabled` / `active` —
#     stand curation. Already write-once (`_apply_category_upsert` sets them
#     only on create) and stripped from every hash (`cf.CURATION_KEYS`): the
#     same cure, two releases earlier, for the same failure.
#   * `is_test` / `deleted` / `tn_parent_id` — the loader owns these outright,
#     and an is_test row is refused before a scalar is written at all.
_OPTIONAL_CATEGORY_SCALARS = (
    "children_as", "children_axis_label", "comment",
    "external_id", "external_source", "translatable",
)

#: Of those, the ones the EXPORT writes only when set (``cf._category_record``).
#: The fixture side has to hash by the same rule — drop an unset value — or a
#: record spelling the default out never equals the row it describes.
_EXPORT_WHEN_SET = ("children_as", "children_axis_label", "external_source")

_OPTIONAL_DEFAULTS: Dict[str, object] = {}


def _optional_defaults() -> Dict[str, object]:
    """What each optional scalar holds on a row nobody has decided for.

    Read from the model's own fields rather than restated here: the "unset"
    value of a column is the column's default, and a second copy of it in this
    module is a copy that can drift.
    """
    if not _OPTIONAL_DEFAULTS:
        from .models import Category

        for key in _OPTIONAL_CATEGORY_SCALARS:
            _OPTIONAL_DEFAULTS[key] = Category._meta.get_field(key).get_default()
    return _OPTIONAL_DEFAULTS


def _normalize_category_record(rec: dict) -> dict:
    """Coerce a fixture category record to the exact shape export writes.

    An optional scalar (see :data:`_OPTIONAL_CATEGORY_SCALARS`) the record does
    not state is left ABSENT here — absence is the instruction "keep the live
    value", and normalizing it to "" is precisely the erasure this guards.
    """
    out = {
        "slug": rec["slug"],
        "parent_slug": rec.get("parent_slug"),
        "name": rec.get("name", ""),
        "catalog_icon": rec.get("catalog_icon", ""),
        "carousel_icon": rec.get("carousel_icon", ""),
        "carousel_enabled": bool(rec.get("carousel_enabled", False)),
        "active": bool(rec.get("active", True)),
        "features": [_normalize_entry(e) for e in rec.get("features", [])],
    }
    defaults = _optional_defaults()
    for key in _OPTIONAL_CATEGORY_SCALARS:
        if key not in rec:
            continue
        value, default = rec[key], defaults[key]
        if value is None:
            value = default
        if isinstance(default, bool):
            value = bool(value)
        elif isinstance(default, str) and not value:
            # "" is the unset value of a blank-able column and stays; on one
            # with a non-blank default (`children_as`) it means that default.
            value = default
        out[key] = value
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
# Source identity — which live row IS this fixture row?
# ---------------------------------------------------------------------------
#
# The fixture files address categories by ``slug`` and always will: the tree
# edges (``parent_slug``) and the sidecar keys are slugs. But for a category
# imported from an external catalogue the slug is *derived* (the source's node
# path transliterated), so the source renaming a node moves the slug while the node
# is the same node. Matching a re-import by slug would then read as "one
# category disappeared, an unrelated one appeared" and leave a duplicate next
# to the row that already holds that node's listings.
#
# So identity precedence is:
#
#   1. ``(external_source, external_id)`` when the fixture row carries one —
#      the row is updated in place, slug included (that IS the rename).
#   2. ``slug`` otherwise — hand-seeded categories have no source identity and
#      the slug is the only key they have.
#
# The pair, not ``external_id`` alone: the id is the source catalogue's own
# numbering, and two catalogues numbering from 1 would silently collapse onto
# each other's rows. ``external_source`` is blank for a single-source catalog
# (and for every row written before the field existed), so the pair degrades
# to plain ``external_id`` matching wherever only one source feeds the tree.


def _identity(record: dict):
    """``(external_source, external_id)`` of a fixture record, or ``None``."""
    ext = str(record.get("external_id") or "").strip()
    if not ext:
        return None
    return str(record.get("external_source") or "").strip(), ext


def _fmt_identity(ident) -> str:
    src, ext = ident
    return f"external_id '{ext}'" + (f", source '{src}'" if src else "")


@dataclass
class _LiveRow:
    pk: int
    slug: str
    identity: Optional[tuple]
    is_test: bool


@dataclass
class _Identities:
    """How each category fixture row resolves against the live table.

    ``renames`` maps a *live* slug to the fixture slug the same source node now
    sits under. ``order_after`` sequences the upserts so a rename whose target
    slug is still held by another row this same load moves away runs second.
    ``problems`` holds the fixture keys that cannot be applied at all, each
    with the message the report prints — never a silent pick.
    """
    renames: Dict[str, str] = field(default_factory=dict)
    order_after: Dict[str, str] = field(default_factory=dict)
    problems: Dict[str, str] = field(default_factory=dict)
    #: Fixture key -> message, for a slug-matched row whose stored identity the
    #: fixture overwrites. Applied (the fixture is canon for its slug), but
    #: never silently: re-pointing a row at another source node is exactly the
    #: kind of edit an operator wants to see in the plan before it runs.
    restamps: Dict[str, str] = field(default_factory=dict)

    def rename_detail(self, fixture_slug: str, record: dict) -> str:
        old = self.renamed_from(fixture_slug)
        if old is None:
            return self.restamps.get(fixture_slug, "")
        return f"renamed: slug '{old}' → '{fixture_slug}' ({_fmt_identity(_identity(record))})"

    def renamed_from(self, fixture_slug: str) -> Optional[str]:
        for old, new in self.renames.items():
            if new == fixture_slug:
                return old
        return None


def _live_categories() -> List[_LiveRow]:
    """Every category row (test/soft-deleted included) as an identity view.

    Deliberately unfiltered: a fixture row must resolve against the row that
    actually occupies its slug, whatever its state — an is_test or soft-deleted
    row still holds the unique slug, and pretending it does not is how a load
    turns into an IntegrityError instead of a per-record message.
    """
    from .models import Category

    rows = []
    for pk, slug, src, ext, is_test in Category.objects.values_list(
        "pk", "slug", "external_source", "external_id", "is_test"
    ):
        ext = (ext or "").strip()
        rows.append(_LiveRow(pk, slug, ((src or "").strip(), ext) if ext else None, is_test))
    return rows


def _resolve_identities(fix_cat: Dict[str, dict]) -> _Identities:
    """Resolve every fixture row to a live row and detect what blocks it."""
    out = _Identities()
    rows = _live_categories()
    by_slug = {r.slug: r for r in rows}
    by_identity: Dict[tuple, List[_LiveRow]] = {}
    for r in rows:
        if r.identity is not None:
            by_identity.setdefault(r.identity, []).append(r)

    for slug, record in fix_cat.items():
        ident = _identity(record)
        if ident is None:
            continue  # hand-seeded row: the slug is the identity, nothing to do
        candidates = by_identity.get(ident, [])
        if len(candidates) > 1:
            # Two live rows claim one source node. Prefer the one already at
            # this slug; if that does not single one out, refuse — picking
            # arbitrarily would move listings under whichever row sorted first.
            exact = [c for c in candidates if c.slug == slug]
            if len(exact) != 1:
                names = ", ".join(sorted(f"'{c.slug}'" for c in candidates))
                out.problems[slug] = (
                    f"{_fmt_identity(ident)} matches {len(candidates)} live "
                    f"categories ({names}) — the catalog holds duplicates for one "
                    "source node; merge or clear them before re-importing"
                )
                continue
            candidates = exact
        if not candidates:
            # Nobody carries this identity yet. The slug fallback may adopt an
            # existing row (a hand-seeded one gaining its source id), but never
            # one that already belongs to a DIFFERENT source node.
            holder = by_slug.get(slug)
            if holder is not None and holder.identity is not None and holder.identity != ident:
                out.restamps[slug] = (
                    f"source identity re-stamped: {_fmt_identity(holder.identity)} "
                    f"→ {_fmt_identity(ident)} on slug '{slug}' — the fixture is "
                    "canon for this slug, but check it is the same node"
                )
            continue
        row = candidates[0]
        if row.slug != slug:
            out.renames[row.slug] = slug

    # A rename can only land if its target slug is free by the time it runs.
    movers = set(out.renames)
    for old, new in out.renames.items():
        holder = by_slug.get(new)
        if holder is None or holder.slug == old:
            continue
        if holder.slug in movers:
            # The holder moves away in this same load — sequence it first.
            out.order_after[new] = out.renames[holder.slug]
        else:
            out.problems[new] = (
                f"source identity ({_fmt_identity(_identity(fix_cat[new]))}) matches "
                f"category '{old}', but its new slug '{new}' is held by a different "
                "category this import does not move — identity wins, so the rename "
                "is refused rather than clobbering that row; rename or hard-delete "
                "it first, then re-run"
            )

    # Chains resolve by ordering; a cycle (two nodes swapping slugs) cannot —
    # there is no order in which both targets are free. Report both ends.
    for key in _cyclic(out.order_after):
        out.problems.setdefault(key, (
            f"rename to slug '{key}' is part of a cycle of source-side renames "
            "(two categories swapping slugs) — no order frees both slugs; "
            "resolve one of them by hand, then re-run"
        ))
    return out


def _cyclic(order_after: Dict[str, str]) -> List[str]:
    """Keys of ``order_after`` that sit on a cycle (Kahn leftovers)."""
    indeg = {k: 0 for k in order_after}
    for k, dep in order_after.items():
        if dep in indeg:
            indeg[k] += 1
    ready = [k for k, d in indeg.items() if d == 0]
    seen = set()
    while ready:
        k = ready.pop()
        seen.add(k)
        for other, dep in order_after.items():
            if dep == k and other not in seen:
                indeg[other] -= 1
                if indeg[other] == 0:
                    ready.append(other)
    return sorted(set(order_after) - seen)


def _remap_by_identity(hashes: dict, idents: _Identities) -> dict:
    """Re-key a slug-keyed map (DB view, sidecar base, live optional values).

    The 3-way diff is keyed by slug; a renamed node's DB and base hashes are
    filed under its OLD slug, so without this the diff reads a rename as a
    delete of the old key plus a create of the new one. Moving the entries
    makes the same key line up on all three sides — the record then classifies
    as an ordinary fast-forward (or conflict), and the plan says "updated",
    which is what actually happens. Applied in dependency order so a chain
    (a→b while b→c) never overwrites an entry that has not moved yet.
    """
    if not idents.renames:
        return hashes
    out = dict(hashes)
    for new in _rename_order(idents):
        old = idents.renamed_from(new)
        if old is None or old not in out:
            continue
        out[new] = out.pop(old)
    return out


def _delay_blocked_renames(planned: List["_Planned"], idents: _Identities) -> List["_Planned"]:
    """Reorder upserts so a slug's current holder moves away before its claimant.

    Kahn over the (already depth-sorted) list with the blocking edges from
    ``_Identities.order_after``: with no edges the output is the input, so the
    ordering the tree needs is untouched whenever nothing was renamed.
    """
    if not idents.order_after:
        return planned
    index = {p.key: i for i, p in enumerate(planned)}
    blockers = {
        k: dep for k, dep in idents.order_after.items()
        if k in index and dep in index
    }
    out: List[_Planned] = []
    emitted: set = set()
    remaining = list(planned)
    while remaining:
        ready = [p for p in remaining if blockers.get(p.key) in (None, *emitted)]
        if not ready:  # cycle — already reported as a problem; keep going
            ready = remaining
        head = min(ready, key=lambda p: index[p.key])
        out.append(head)
        emitted.add(head.key)
        remaining.remove(head)
    return out


def _rename_order(idents: _Identities) -> List[str]:
    """Fixture keys of every rename, dependency-ordered (holders first)."""
    keys = sorted(idents.renames.values())
    ordered: List[str] = []
    placed = set()
    # Simple fixpoint: keys whose blocker is already placed (or absent) go next.
    # Bounded by len(keys) passes; a cycle is reported as a problem, and its
    # members are appended in slug order so the loop always terminates.
    for _ in range(len(keys)):
        progressed = False
        for key in keys:
            if key in placed:
                continue
            dep = idents.order_after.get(key)
            if dep is None or dep in placed:
                ordered.append(key)
                placed.add(key)
                progressed = True
        if not progressed:
            break
    ordered.extend(k for k in keys if k not in placed)
    return ordered


# ---------------------------------------------------------------------------
# Feature renames — the half of a slug rename that lives outside this module
# ---------------------------------------------------------------------------
#
# A feature's SLUG is not a label. It is the key every listing files its answer
# under (``stapel_listings.Listing.features_draft`` is ``{slug: value}``), so
# renaming one here moves the schema and strands every stored answer under a
# key the schema no longer knows. Measured on a live fleet on 2026-09-05:
# ``load_catalog --on-conflict fixture-wins`` applied a fixture in which five
# car features had new slugs (``make_ref_select`` → ``make``,
# ``body_type_ref_select`` → ``body_type``, and three more). The dry run said
# ``features: updated 62`` and not one word about a rename. Afterwards the make
# facet was empty, the search projection had lost the values, and
# ``listings_reproject_features`` — which keys on the CURRENT slugs — would have
# DROPPED them rather than repaired them.
#
# The loader renamed silently, so three things change:
#
# 1. renames are DETECTED and named, in the dry run and in the apply alike;
# 2. a plain apply REFUSES them — the live slug is kept and the rename is
#    reported as blocked — because a rename is a two-sided data migration and
#    this side alone cannot perform it;
# 3. ``--rename-features`` performs it, and calls the hook that performs the
#    other side (``FEATURE_RENAME_HOOK``, by default
#    ``listings.rename_feature_keys``) once per category. With no hook
#    reachable the renames stay blocked unless the operator ALSO passes
#    ``--no-hook``, which is the explicit statement "there are no listings
#    behind this catalogue" — nobody should be able to make that statement by
#    forgetting to install something.

#: The comm Function ``--rename-features`` calls when ``FEATURE_RENAME_HOOK``
#: is left at ``"auto"`` and stapel-listings is installed beside us.
DEFAULT_FEATURE_RENAME_HOOK = "listings.rename_feature_keys"


def _feature_identity(record: dict):
    """What makes two feature records the SAME feature under different slugs.

    The source's own id where there is one — the ``(external_source,
    external_id)`` pair, exactly how a category is matched — and the display
    NAME otherwise, case-folded. ``Feature`` carries no external identity
    column today, so in practice this is the name; the id branch is here
    because the export record is the one place a source id would arrive, and a
    detector that could only ever see names would have to be rewritten the day
    one does.

    ``None`` for a record with neither: an unnamed feature is not evidence of
    anything, and guessing a rename from an empty identity is how a repair pass
    rewrites the wrong listings.
    """
    ext = str(record.get("external_id") or "").strip()
    if ext:
        return ("id", str(record.get("external_source") or "").strip(), ext)
    name = str(record.get("name") or "").strip()
    return ("name", name.casefold()) if name else None


@dataclass
class _FeatureRenames:
    """Every ``old_slug -> new_slug`` this load would perform, and where.

    ``by_category`` is what the hook is called with — one map per category, so
    the module that owns the listings is told exactly which subtree moved.
    ``pairs`` is the same information flattened, for the plan the operator
    reads and for the blocking substitution. ``notes`` holds the pairs that
    LOOK like renames and are refused as ambiguous: a rename applied to the
    wrong feature rewrites sellers' answers into the wrong field, so anything
    short of a one-to-one match is reported and left alone.
    """
    by_category: Dict[str, Dict[str, str]] = field(default_factory=dict)
    pairs: Dict[str, str] = field(default_factory=dict)
    notes: List[Item] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.pairs)

    def blocked_notes(self) -> Dict[str, str]:
        """``{feature slug: why this record is not being written}``.

        Both ends of every pair: the new slug must not be created (nothing
        would reference it) and the old one must not be deleted (every stored
        answer is still filed under it).
        """
        out: Dict[str, str] = {}
        for old, new in self.pairs.items():
            out[new] = (
                f"feature rename BLOCKED: '{old}' → '{new}' is a slug rename, and "
                "every listing in these categories still answers under the old "
                "slug. Re-run with --rename-features to apply it and move the "
                "stored answers with it"
            )
            out[old] = (
                f"feature rename BLOCKED: kept, because '{new}' would strand the "
                f"answers stored under '{old}'"
            )
        return out

    def describe(self) -> str:
        return ", ".join(f"{old} → {new}" for old, new in sorted(self.pairs.items()))


def _entry_slugs(record: dict) -> set:
    """Slugs a category record's feature list references, malformed rows aside."""
    return {
        e["slug"] for e in (record.get("features") or ())
        if isinstance(e, dict) and e.get("slug")
    }


def _detect_feature_renames(fix_feat, fix_cat, db_feat, db_cat) -> _FeatureRenames:
    """Which features this fixture renames, per category, by identity.

    A rename is a feature that LEAVES a category's list and one that JOINS it
    carrying the same identity — the only shape in which "the same feature
    under a new slug" can be seen from two record sets. Everything else (a
    feature genuinely removed, a genuinely new one) has no counterpart on the
    other side and is left to the ordinary plan.

    Deliberately narrow. A pair is a rename only when the old slug is a live
    root the fixture no longer defines and the new slug is not a live root
    already, and when the identities match ONE to ONE on both sides. Two
    features sharing a name inside one category, or one identity arriving
    under two slugs, is reported as ambiguous and applied as neither: the cost
    of a wrong rename is other people's data written into the wrong field.
    """
    out = _FeatureRenames()
    # Identities that turned out ambiguous ANYWHERE. A feature root is shared
    # by every category that lists it, so an identity nobody can resolve under
    # one category is not resolvable under another either — a pair that looked
    # one-to-one over there would move the same root.
    poisoned: set = set()
    ident_of: Dict[str, tuple] = {}
    for key, record in sorted(fix_cat.items()):
        live = db_cat.get(key)
        if live is None:
            continue  # a category being created holds no listings to strand
        fixture_slugs = _entry_slugs(record)
        live_slugs = _entry_slugs(live)
        gone = sorted(live_slugs - fixture_slugs)
        arrived = sorted(fixture_slugs - live_slugs)
        if not gone or not arrived:
            continue

        old_by_ident: Dict[tuple, List[str]] = {}
        for slug in gone:
            if slug not in db_feat:
                continue  # not a root of ours; nothing to rename
            ident = _feature_identity(db_feat[slug])
            if ident is not None:
                old_by_ident.setdefault(ident, []).append(slug)
        new_by_ident: Dict[tuple, List[str]] = {}
        for slug in arrived:
            if slug in db_feat or slug not in fix_feat:
                continue  # the slug is already a live root, or has no definition
            ident = _feature_identity(fix_feat[slug])
            if ident is not None:
                new_by_ident.setdefault(ident, []).append(slug)

        for ident, olds in sorted(old_by_ident.items()):
            news = new_by_ident.get(ident)
            if not news:
                continue
            if len(olds) > 1 or len(news) > 1:
                poisoned.add(ident)
                out.notes.append(Item(RENAME_BLOCKED, key, (
                    f"{len(olds)} feature(s) leaving and {len(news)} arriving share "
                    f"one identity ({ident[-1]}) — refusing to guess which is a "
                    "rename of which; give them distinct names, or rename them "
                    "one at a time"
                )))
                continue
            old, new = olds[0], news[0]
            seen = out.pairs.get(old)
            if seen is not None and seen != new:
                out.notes.append(Item(RENAME_BLOCKED, key, (
                    f"'{old}' renames to '{seen}' under another category and to "
                    f"'{new}' here — a root feature is shared, so it cannot move "
                    "to two slugs; split it first"
                )))
                continue
            out.pairs[old] = new
            ident_of[old] = ident
            out.by_category.setdefault(key, {})[old] = new

    # One identity arriving under two different slugs across categories is the
    # mirror image of the check above and just as unresolvable.
    targets: Dict[str, str] = {}
    for old, new in sorted(out.pairs.items()):
        clash = targets.get(new)
        if clash is not None:
            poisoned.add(ident_of[old])
            poisoned.add(ident_of[clash])
            out.notes.append(Item(RENAME_BLOCKED, new, (
                f"both '{clash}' and '{old}' would rename to '{new}' — refusing "
                "to merge two features into one slug"
            )))
        targets[new] = old

    # An identity that could not be resolved under ONE category is dropped
    # everywhere. The alternative — renaming the shared root because some other
    # category happened to see a clean one-to-one — is the wrong-field write
    # this whole detector exists to avoid.
    for old, ident in sorted(ident_of.items()):
        if ident not in poisoned:
            continue
        out.pairs.pop(old, None)
        for mapping in out.by_category.values():
            mapping.pop(old, None)
    out.by_category = {k: v for k, v in out.by_category.items() if v}
    return out


def _unrename_categories(fix_cat: Dict[str, dict], renames: _FeatureRenames):
    """Fixture category records with every blocked rename put back to the live slug.

    Blocking the feature records alone would leave the category records
    referencing a slug no root defines, and every one of them would fail with
    a dangling reference — turning a refusal into a wall of errors. So the
    entries are read back to the slug the catalogue actually holds: the load
    applies everything else in the record and the categories keep the feature
    they have. The substitution is by slug across the WHOLE fixture, not only
    the categories the rename was detected under, because the new root is not
    being created anywhere.
    """
    back = {new: old for old, new in renames.pairs.items()}
    out = dict(fix_cat)
    for key, record in fix_cat.items():
        entries = record.get("features") or ()
        if not (_entry_slugs(record) & set(back)):
            continue
        out[key] = {
            **record,
            "features": [
                {**e, "slug": back[e["slug"]]}
                if isinstance(e, dict) and (e.get("slug") or "") in back
                else e
                for e in entries
            ],
        }
    return out


def _resolve_rename_hook():
    """The comm Function name ``--rename-features`` should call, or ``None``.

    ``"auto"`` (the default) means "the listings library if it is installed":
    a deployment that has one must move its stored answers, and a deployment
    that has none has nothing to move. An explicit name overrides; an empty
    value, or ``"none"``, says there is no second half — which the loader then
    makes the operator confirm at the command line rather than infer.
    """
    from .conf import categories_settings

    name = (categories_settings.FEATURE_RENAME_HOOK or "").strip()
    if not name or name.lower() == "none":
        return None
    if name.lower() != "auto":
        return name
    import importlib.util

    if importlib.util.find_spec("stapel_listings") is not None:
        return DEFAULT_FEATURE_RENAME_HOOK
    return None


def _call_rename_hook(report, hook: str, by_category: Dict[str, Dict[str, str]]) -> None:
    """Perform the other half of the migration, once per renamed category.

    After the transaction, deliberately: the hook is a comm call that may cross
    a process boundary and write another module's tables, and holding this
    module's subtree lock open across it is how a catalogue import becomes a
    deadlock. The catalogue is already committed if the hook fails — which is
    why the failure is reported per category with the map it was called with,
    so the operator can replay exactly that call by hand.
    """
    from stapel_core.comm import call

    from .models import Category

    ids = dict(
        Category.objects.filter(slug__in=list(by_category)).values_list("slug", "pk")
    )
    for slug, mapping in sorted(by_category.items()):
        entry = {"category": slug, "renames": dict(mapping)}
        category_id = ids.get(slug)
        if category_id is None:
            entry["error"] = "category not found after apply — nothing was called"
            report.rename_hook_results.append(entry)
            continue
        try:
            entry["result"] = call(
                hook,
                {"category_id": category_id, "renames": dict(mapping), "dry_run": False},
            )
        except Exception as exc:  # comm is a network boundary; never fatal here
            entry["error"] = f"{exc.__class__.__name__}: {exc}"
        report.rename_hook_results.append(entry)


# ---------------------------------------------------------------------------
# Apply helpers — always through .save()/.full_clean()
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _deferred_tree_rebuild(*models):
    """Rebuild django-treenode's denormalized columns ONCE per load, not per row.

    django-treenode keeps ``tn_ancestors_pks`` / ``tn_depth`` / ``tn_order`` and
    friends up to date from a ``post_save`` / ``post_delete`` receiver that
    rebuilds the **entire** table: one read of every row plus one ``UPDATE`` per
    row, for every single row written. A load of N rows therefore costs O(N²)
    statements against a heap that cannot be vacuumed inside the load's
    transaction, so the real curve is worse than quadratic. Measured on the
    imported catalog fixtures (postgres 16, one transaction, no deletes):

                                        before   after
        32 features / 3 categories       0.6 s    0.4 s
        64 features / 4 categories       1.5 s    0.7 s
       134 features / 8 categories       5.1 s    1.4 s
       240 features / 17 categories     63.3 s    3.8 s
       430 features / 51 categories     killed    6.0 s
     14409 features / 3444 categories   killed  185.2 s

    Suspending the receivers for the write phase and rebuilding once at the end
    is exactly what treenode itself does inside its own bulk operations
    (``TreeNodeModel.delete_tree`` / ``update_tree`` use ``no_signals()``), and
    it changes no row's final state: ``update_tree`` is a pure function of the
    ``tn_parent`` edges, which the writes have already committed by then.

    What this does NOT suspend is anything the loader's H-2 rule is about —
    ``full_clean``, ``save``, the revision bump, ``category.changed`` and
    ``copy_parent_features`` all still run per row, because they are model and
    stapel receivers, not treenode's.

    The rebuild runs only on a clean exit, and inside the caller's transaction,
    so a failed load rolls the denormalized columns back with the rows.
    """
    from treenode.signals import connect_signals, disconnect_signals

    disconnect_signals()
    try:
        yield
        # treenode's own delete() re-arms the receivers on its way out (its
        # no_signals() exits by connecting), so a hard-delete phase can leave
        # them live. Idempotent either way.
        disconnect_signals()
        for model in models:
            model.update_tree()
    finally:
        connect_signals()


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
    "show_as_badge", "show_at_title", "visibility", "translate",
    "rules", "description", "example", "default", "hints", "group",
    "is_test", "deleted",
)
_CATEGORY_SCALARS = (
    "slug", "name", "external_id", "external_source", "comment", "catalog_icon",
    "carousel_icon", "carousel_enabled", "active", "translatable", "is_test",
    "deleted", "tn_parent_id", "children_as", "children_axis_label",
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
    for attr, value in _disclosure(record).items():
        setattr(feat, attr, value)
    feat.translate = record.get("translate", "all")
    feat.rules = record.get("rules") or []
    feat.description = record.get("description", "")
    feat.example = record.get("example", "")
    feat.default = record.get("default")
    feat.hints = record.get("hints") or []
    feat.group = record.get("group", "")
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


#: "This entry says nothing about that field" — distinct from a stored ``None``,
#: which ``default`` uses as a real value ("the form starts empty").
_UNSET = object()


def _entry_matches(feat, desired: dict) -> bool:
    return all(
        v is _UNSET or getattr(feat, k) == v for k, v in desired.items()
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
        # A slug-less row always states its identity; a slug-bearing override
        # states a name only when it differs from the root's — so on that side
        # ABSENT means "the root's name", the exact rule the export writes by
        # (``_feature_list_entry``). It used to mean "leave whatever is there",
        # which is not the same thing once the ROOT is renamed: the clone kept
        # the old label, export then wrote it out, and the record's DB hash
        # could never equal its fixture hash again — 112 of the 303 categories
        # that re-planned "updated" forever on one live catalogue, each of them
        # also showing a seller the label canon had already retired.
        "name": entry.get("name", "") if not slug else entry.get("name", root.name),
        "icon": entry.get("icon", "") if not slug else _UNSET,
        "comment": entry.get("comment", "") if not slug else _UNSET,
        "config": entry.get("config") or {},
        "mandatory": bool(entry.get("mandatory", False)),
        **_disclosure(entry),
        "translate": entry.get("translate", "all"),
        "rules": entry.get("rules") or [],
        "description": entry.get("description", ""),
        "example": entry.get("example", ""),
        "default": entry.get("default"),
        "hints": entry.get("hints") or [],
        "group": entry.get("group", ""),
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
                if v is not _UNSET and getattr(feat, k) != v:
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
        if v is not _UNSET:
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


def _match_category(record: dict):
    """The live row this fixture record IS, and whether identity chose it.

    Re-queried at apply time rather than reusing the planning pass: earlier
    upserts in this same run may have moved slugs, and the row that answers
    here is the row the write will touch.
    """
    from .models import Category

    slug = record["slug"]
    ident = _identity(record)
    if ident is not None:
        rows = list(
            Category.objects.filter(
                external_source=ident[0], external_id=ident[1]
            ).order_by("pk")
        )
        if len(rows) > 1:
            exact = [r for r in rows if r.slug == slug]
            if len(exact) != 1:
                names = ", ".join(sorted(f"'{r.slug}'" for r in rows))
                raise RecordError(
                    f"{_fmt_identity(ident)} matches {len(rows)} live categories "
                    f"({names}) — merge or clear the duplicates before re-importing"
                )
            rows = exact
        if rows:
            return rows[0], True
        # No row carries this identity: fall through to the slug. That row may
        # be hand-seeded (adopting its first source id) or may already carry a
        # different one (a re-stamp) — the fixture is canon for its own slug,
        # and _Identities.restamps makes the second case visible in the plan.
        return Category.objects.filter(slug=slug).first(), False
    return Category.objects.filter(slug=slug).first(), False


def _apply_category_upsert(record: dict):
    from .models import Category

    slug = record["slug"]
    existing, by_identity = _match_category(record)
    if existing is not None and existing.is_test:
        raise RecordError(
            f"category slug '{slug}' is occupied by an is_test row — not overwriting"
        )
    if by_identity and existing.slug != slug:
        # A source-side rename: the row keeps its pk (and its listings), the
        # slug moves with it. The slug is globally unique, so check first —
        # identity won the match, and that must not turn into clobbering
        # whoever holds the new slug.
        blocker = Category.objects.filter(slug=slug).exclude(pk=existing.pk).first()
        if blocker is not None:
            held_by = (
                _fmt_identity((blocker.external_source, blocker.external_id))
                if blocker.external_id else "a hand-seeded category"
            )
            raise RecordError(
                f"{_fmt_identity(_identity(record))} matches category "
                f"'{existing.slug}', but its new slug '{slug}' is held by {held_by} "
                "— identity wins the match, so the rename is refused rather than "
                "clobbering that row; rename or hard-delete it first, then re-run"
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
    # Optional scalars: a key the record does not state says nothing, so the
    # live value stands (see _OPTIONAL_CATEGORY_SCALARS). A stated one is
    # written, `""` / `auto` included — that is a fixture CLEARING a column.
    defaults = _optional_defaults()
    for key in _OPTIONAL_CATEGORY_SCALARS:
        if key in record:
            setattr(cat, key, record[key])
        elif created:
            setattr(cat, key, defaults[key])
    if created:
        # Curation is stand-owned (cf.CURATION_KEYS): the fixture seeds it on
        # a fresh row (an export→restore keeps its curation), but an update
        # leaves whatever the operator set — a catalogue re-import resetting
        # carousel_enabled to False is how a live stand's home screen lost
        # its tiles, and re-activating a leaf the operator had switched off
        # is how the same stand got two untyped dead ends and a duplicate
        # sibling back in front of sellers. The 3-way hash ignores these keys
        # on both sides, so this branch is also what keeps the diff honest: a
        # record never classifies as changed *because* of a key an update
        # would then refuse to write. tn_priority is not in the record at
        # all — same ownership, enforced one layer earlier.
        cat.catalog_icon = record.get("catalog_icon", "")
        cat.carousel_icon = record.get("carousel_icon", "")
        cat.carousel_enabled = bool(record.get("carousel_enabled", False))
        cat.active = bool(record.get("active", True))
    # `children_as` / `children_axis_label` are catalogue content, not stand
    # curation: a partition of one template is a partition wherever the
    # catalogue is loaded, so a STATED value is written here like any other.
    # An operator who authored a different one has changed the DB side, and
    # the 3-way diff reports a conflict rather than this loop quietly winning.
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
    note: str = ""                  # extra report detail (e.g. the rename)
    renamed: bool = False
    #: The hash of the fixture side as this plan read it — half of the pair a
    #: successful apply writes back to the sidecar (see _new_base).
    fixture_hash: Optional[str] = None


def _plan_side(fix: dict, base: dict, db_hashes: dict, *, on_conflict, deletions,
               idents: Optional[_Identities] = None, hash_view=None,
               blocked: Optional[Dict[str, str]] = None):
    """Classify every natural key on one side (features or categories).

    ``hash_view`` (categories only) maps a record to the projection that gets
    content-hashed — the presentation strip (``cf.category_sync_view``). It
    must match how the OTHER two sides were hashed: ``build_catalog`` applies
    the same view to the DB state, and the sidecar base was written from one
    of those two, so all three hashes describe the same subset of the record.
    The full record still rides ``_Planned.record`` — an upsert that CREATES
    the row applies the presentation keys the hash ignores.
    """
    keys = set(fix) | set(base) | set(db_hashes)
    planned: List[_Planned] = []
    for key in sorted(keys):
        if blocked and key in blocked:
            # A slug rename this load refuses to perform: never written, never
            # deleted, and the base is left where it was so the record stays
            # visible on every subsequent run until somebody decides.
            planned.append(_Planned(
                key, Decision("note", RENAME_BLOCKED), fix.get(key),
                note=blocked[key],
            ))
            continue
        if idents is not None and key in idents.problems:
            # Unresolvable identity: never planned as a write. The report
            # carries the reason and the run exits non-zero.
            planned.append(_Planned(
                key, Decision("error", ERROR, conflict=True), fix.get(key),
                note=idents.problems[key],
            ))
            continue
        if key in fix:
            f_hash = cf.content_hash(hash_view(fix[key]) if hash_view else fix[key])
        else:
            f_hash = None
        d_hash = db_hashes.get(key)
        raw = _classify(base.get(key), f_hash, d_hash)
        decision = _decide(
            raw,
            db_present=d_hash is not None,
            on_conflict=on_conflict,
            deletions=deletions,
        )
        note, renamed = "", False
        if idents is not None and key in fix:
            note = idents.rename_detail(key, fix[key])
            renamed = idents.renamed_from(key) is not None
        planned.append(_Planned(key, decision, fix.get(key), note=note,
                                renamed=renamed, fixture_hash=f_hash))
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
    rename_features: bool = False,
    call_hook: bool = True,
):
    """Reconcile the fixtures in ``directory`` into the live catalog.

    Returns a :class:`Report`. Writes the updated ``.sync-state.json`` sidecar
    on a real (non ``dry_run``) run. Raises nothing for conflicts — the caller
    inspects ``report.failed`` for the exit code.

    ``rename_features`` allows the one class of change this loader refuses by
    default: a feature-slug rename, which moves the key every listing files its
    answer under and therefore has a second half outside this module (see the
    "Feature renames" section). With ``call_hook`` (the default) that second
    half is performed by ``FEATURE_RENAME_HOOK`` — and if no hook is reachable
    the renames stay blocked, because applying them alone is precisely the
    incident. ``call_hook=False`` is the operator saying out loud that there
    are no listings behind this catalogue.
    """
    from .models import Category, Feature

    fix_feat, fix_cat, base = _load_inputs(directory)
    if base is not None and base.get("version") not in cf.SUPPORTED_STATE_VERSIONS:
        raise ValueError(
            f"incompatible .sync-state.json version {base.get('version')!r} "
            f"(expected one of {', '.join(str(v) for v in cf.SUPPORTED_STATE_VERSIONS)}); "
            "regenerate via export_catalog"
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

    # Resolved before the plan, not after it: whether the other half of a
    # rename can be performed at all is what decides whether this half may be.
    hook = _resolve_rename_hook() if call_hook else None
    apply_renames = bool(rename_features) and (not call_hook or hook is not None)

    if dry_run:
        _run_plan(
            report, fix_feat, fix_cat, base_feat, base_cat,
            on_conflict=on_conflict, deletions=deletions, apply=False,
            apply_renames=apply_renames, rename_features=rename_features,
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
            apply_renames=apply_renames, rename_features=rename_features,
        )

    # The sidecar reflects the applied state — written after commit.
    with open(os.path.join(directory, cf.STATE_FILE), "w", encoding="utf-8") as fh:
        fh.write(cf.canonical_json(new_state))

    # …and only then the other half of a rename. After the commit and after the
    # sidecar: the schema move is durable either way, and a hook that fails must
    # cost the operator a replay, not the record of what was applied.
    if report.feature_renames_applied and hook and report.feature_renames_by_category:
        report.rename_hook = hook
        _call_rename_hook(report, hook, report.feature_renames_by_category)
    return report


def _run_plan(
    report, fix_feat, fix_cat, base_feat, base_cat, *, on_conflict, deletions, apply,
    apply_renames: bool = False, rename_features: bool = False,
):
    """Classify and (optionally) apply, in referential order.

    Order: feature upserts → category upserts (parents first) → category
    deletes → feature deletes. Deletes come last so a row is never removed
    while something still references it. Returns the new sidecar state (or
    ``None`` for a dry run).
    """
    from .models import Category, Feature

    # DB view (excludes is_test + soft-deleted, exactly like export). The
    # RECORDS as well as the hashes: the hashes drive the 3-way diff, and the
    # records are what a rename is read off (which feature a category lists,
    # and under which name).
    db_feat_records, db_cat_records, db_state = cf.build_catalog(include_test=False)
    db_feat = db_state["features"]
    db_cat = db_state["categories"]
    db_feat_by_slug = {r["slug"]: r for r in db_feat_records}
    db_cat_by_slug = {r["slug"]: r for r in db_cat_records}

    # Source identity first: a node the source renamed sits in the DB view (and
    # in the sidecar base) under its OLD slug. Re-keying those two onto the new
    # slug is what turns "delete a + create b" into "update a in place" — for
    # the plan the operator reads AND for the writes that follow.
    idents = _resolve_identities(fix_cat)
    db_cat = _remap_by_identity(db_cat, idents)
    base_cat = _remap_by_identity(base_cat, idents)
    # The live values an unsaid key keeps — re-keyed onto the renames with
    # everything else, or a renamed node would read its predecessor's blanks.
    db_optional = _remap_by_identity(_db_optional_values(), idents)
    report.kept_unsaid = _kept_unsaid(fix_cat, db_optional)

    # A slug rename is the one change this loader will not make by itself: the
    # slug is the key every listing files its answer under, so moving it here
    # and nowhere else strands them all. Detected first, because a blocked
    # rename changes what the whole plan below is planning.
    renames = _detect_feature_renames(
        fix_feat, fix_cat, db_feat_by_slug,
        _remap_by_identity(db_cat_by_slug, idents),
    )
    report.feature_renames = dict(renames.pairs)
    report.feature_renames_by_category = {
        key: dict(mapping) for key, mapping in renames.by_category.items()
    }
    report.feature_renames_applied = bool(renames) and apply_renames
    for item in renames.notes:
        report.add("features", item)
    blocked = {} if apply_renames else renames.blocked_notes()
    if blocked:
        fix_cat = _unrename_categories(fix_cat, renames)
    if renames and rename_features and not apply_renames:
        # Asked for, and still refused: there is nothing to perform the other
        # half with. Saying "renamed" here would be the incident again, with a
        # flag as its alibi.
        report.add("features", Item(RENAME_BLOCKED, "*", (
            "--rename-features was passed but no rename hook is reachable "
            f"(STAPEL_CATEGORIES['FEATURE_RENAME_HOOK'], default {DEFAULT_FEATURE_RENAME_HOOK!r} "
            "when stapel-listings is installed) — the renames are still blocked. "
            "Install/point the hook, or pass --no-hook to state that no listings "
            "stand behind this catalogue"
        )))

    feat_plan = _plan_side(
        fix_feat, base_feat, db_feat, on_conflict=on_conflict, deletions=deletions,
        blocked=blocked,
    )
    cat_view = _fixture_hash_view(_root_names_after(fix_feat, feat_plan), db_optional)
    cat_plan = _plan_side(
        fix_cat, base_cat, db_cat, on_conflict=on_conflict, deletions=deletions,
        idents=idents, hash_view=cat_view,
    )
    # A db_new row under a fixture-owned parent is duplicate-shaped — say so,
    # in the same plan the operator reads (dry run and apply alike).
    _flag_db_new_in_canon(cat_plan, fix_cat)

    # Category upserts must run parents-before-children (a child's create needs
    # its parent to exist and to have its features already reconciled so
    # copy_parent_features copies the right rows).
    cat_depth = cf._depths_by_slug(list(fix_cat.values()))
    cat_upserts = sorted(
        (p for p in cat_plan if p.decision.op == "upsert"),
        key=lambda p: (cat_depth.get(p.key, 0), p.key),
    )
    # …then delayed, never reordered, where a rename's target slug is still
    # held by a row this same load moves away (a→b while b→c). Only the
    # blocked record moves, so the depth order above is preserved everywhere
    # else — and a delay can never put a child before its parent.
    cat_upserts = _delay_blocked_renames(cat_upserts, idents)

    # Category deletes run children-first (deepest first, by the LIVE tree):
    # with --deletions hard the parent's guard requires its children to be
    # gone already, so a whole-subtree removal deletes leaves upward.
    db_depths = _db_category_depths()
    cat_deletes = sorted(
        (p for p in cat_plan if p.decision.op == "delete"),
        key=lambda p: (-db_depths.get(p.key, 0), p.key),
    )

    if apply:
        # One tree rebuild for the whole load, not one per row — see
        # _deferred_tree_rebuild. Everything else about a write is unchanged:
        # full_clean, revision bump, category.changed, copy_parent_features.
        with _deferred_tree_rebuild(Feature, Category):
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

        # Post-apply health of the tree the load just produced: sibling name
        # collisions, whichever side each colliding row came from — and the
        # active leaves that type nothing (see dead_end_leaves).
        for item in _sibling_name_collisions():
            report.add("categories", item)
        report.dead_end_leaves = dead_end_leaves()
        report.resurrected = active_under_inactive_parent()

        # Sidecar reflects the applied state. Deliberately NO "max_revision":
        # that key is export's pre-filter base ("has the DB changed since the
        # last EXPORT"). If a load wrote the post-load max here, the very next
        # export_catalog — including the one the db-only-drift warning tells
        # the operator to run — would see an unmoved max(revision) and silently
        # skip, stranding the drift out of canon forever.
        feats_after, cats_after, db_after = cf.build_catalog(include_test=False)
        # …and the honesty half of the convergence rule: a record whose applied
        # row still cannot hash equal to its fixture is recorded as synced (it
        # will not re-plan) — so it is SAID ONCE, here, with the keys that
        # differ. A base that quietly absorbs a difference nobody was told
        # about is a gate that proves nothing.
        _report_residuals(report, "features", feat_plan, db_after["features"],
                          {r["slug"]: r for r in feats_after}, None)
        _report_residuals(report, "categories", cat_plan, db_after["categories"],
                          {r["slug"]: r for r in cats_after}, cat_view)
        return {
            "version": cf.STATE_VERSION,
            "features": _new_base(base_feat, feat_plan, db_after["features"], report, "features"),
            "categories": _new_base(base_cat, cat_plan, db_after["categories"], report, "categories"),
        }

    # dry run: just report intended outcomes
    for side, plan in (("features", feat_plan), ("categories", cat_plan)):
        for p in plan:
            report.add(side, _item(p))
    # The collisions that already exist — a dry run cannot see the post-apply
    # tree, but a clash the operator can fix before loading belongs in the plan.
    for item in _sibling_name_collisions():
        report.add("categories", item)
    # …and the writes this plan would attempt and the model would refuse. An
    # apply discovers these by failing; a plan that stayed silent about them
    # is a gate that proves nothing.
    for item in _unwritable_override_items(fix_feat, feat_plan, cat_upserts):
        report.add("categories", item)
    return None


def _root_names_after(fix_feat, feat_plan) -> dict:
    """``{feature slug: the root's name once this plan is applied}``.

    Same shape as :func:`_root_types_after`, for the one key whose EXPORTED
    presence depends on another record: an override's ``name`` is written only
    when it differs from its root's (``cf._feature_list_entry``). The fixture
    side has to hash by the same rule or a category stating the root's own
    label for an override hashes differently from the row it just wrote —
    forever (see :func:`_fixture_hash_view`).
    """
    from .models import Feature

    names = dict(
        Feature.objects.filter(
            tn_parent__isnull=True, deleted=False, is_test=False
        ).values_list("slug", "name")
    )
    for planned in feat_plan:
        if planned.decision.op != "upsert":
            continue
        record = fix_feat.get(planned.key)
        if record is not None:
            names[planned.key] = record.get("name", "")
    return names


def _db_optional_values() -> Dict[str, dict]:
    """``{slug: {optional scalar: the live value}}`` — one query for the load.

    The values an unsaid fixture key keeps. Keyed by slug, the same space the
    DB hashes and the sidecar base use, so ``_remap_by_identity`` moves them
    onto a rename with everything else.
    """
    from .models import Category

    keys = _OPTIONAL_CATEGORY_SCALARS
    rows = Category.objects.filter(deleted=False, is_test=False).values_list(
        "slug", *keys
    )
    return {row[0]: dict(zip(keys, row[1:])) for row in rows}


def _optional_projection(record: dict, live, defaults: dict) -> dict:
    """The optional scalars of one record as the DB side spells them.

    Hashing only — the record the apply phase reads is untouched.

    An UNSAID key is hashed as the value it will KEEP, so "the fixture does
    not state it" classifies as unchanged instead of as an update that blanks
    the column. A stated key is hashed as itself, dropped where the export
    would write nothing at all (:data:`_EXPORT_WHEN_SET`) — which is both what
    makes a fixture spelling out the default hash as the row it describes, and
    what lets an explicit ``""`` converge in one pass: once the column is
    cleared, the two sides agree instead of differing forever.
    """
    out = dict(record)
    for key in _OPTIONAL_CATEGORY_SCALARS:
        default = defaults[key]
        if key in record:
            value = record[key]
        elif live is not None:
            value = live.get(key, default)
        else:
            value = default
        if key in _EXPORT_WHEN_SET and value == default:
            out.pop(key, None)
        else:
            out[key] = value
    return out


def _kept_unsaid(fix_cat: Dict[str, dict], db_optional: Dict[str, dict]) -> Dict[str, int]:
    """``{key: how many records left it unsaid over a live value}``.

    Only where the live value is not the field's default: keeping a default is
    keeping nothing, and a summary counting those would report a number the
    size of the catalogue on a stand where nothing was ever derived.
    """
    defaults = _optional_defaults()
    kept: Dict[str, int] = {}
    for slug, record in fix_cat.items():
        live = db_optional.get(slug)
        if live is None:
            continue
        for key in _OPTIONAL_CATEGORY_SCALARS:
            if key in record:
                continue
            if live.get(key, defaults[key]) != defaults[key]:
                kept[key] = kept.get(key, 0) + 1
    return kept


def _fixture_hash_view(root_names, db_optional):
    """The category projection the FIXTURE side is hashed through.

    ``cf.category_sync_view`` (stand-owned keys stripped, as on the DB side)
    plus two rules that both say the same thing — a fixture is hashed by what
    it MEANS, not by which keys it happens to spell:

    * the override-name rule above: an entry naming its override exactly as
      its root names itself says nothing the root does not already say, and
      the export writes no ``name`` there — so neither does the hash;
    * :func:`_optional_projection`: a key the record leaves unsaid is hashed
      as the live value it keeps, so it produces no ``updated`` row.

    The record the apply phase reads is untouched; only the projection is.
    """
    defaults = _optional_defaults()

    def view(record: dict) -> dict:
        entries = record.get("features") or ()
        stripped, changed = [], False
        for entry in entries:
            slug = entry.get("slug") or ""
            if slug and "name" in entry and entry["name"] == root_names.get(slug):
                entry = {k: v for k, v in entry.items() if k != "name"}
                changed = True
            stripped.append(entry)
        if changed:
            record = {**record, "features": stripped}
        record = _optional_projection(
            record, db_optional.get(record.get("slug")), defaults
        )
        return cf.category_sync_view(record)
    return view


def _root_types_after(fix_feat, feat_plan) -> dict:
    """``{feature slug: the root's config.type once this plan is applied}``.

    A root the plan will upsert takes the fixture's type; every other root
    keeps the type the DB holds now. Roots absent from both are absent here —
    a dangling reference is a different finding, reported at apply time.
    """
    from .models import Feature

    types = {
        slug: (config or {}).get("type")
        for slug, config in Feature.objects.filter(
            tn_parent__isnull=True, deleted=False, is_test=False
        ).values_list("slug", "config")
    }
    for planned in feat_plan:
        if planned.decision.op != "upsert":
            continue
        record = fix_feat.get(planned.key)
        if record is not None:
            types[planned.key] = (record.get("config") or {}).get("type")
    return types


def _unwritable_override_items(fix_feat, feat_plan, cat_upserts) -> List[Item]:
    """The overrides ``Feature.clean`` would refuse, found before writing any.

    A per-category override is a CHILD of the root that shares its slug, and
    the model requires the two to agree on ``config.type``. The fixture is
    self-consistent about that by construction; the DB is not, because a root
    can be retyped by anything else that writes the catalog — another fixture
    directory sharing this feature-slug namespace, the feature editor, an
    admin. When that root is left as it is (db-only drift the policy does not
    revert, or a conflict the policy aborts), every category record carrying
    an override for it is refused, the same records on every pass.
    """
    after = _root_types_after(fix_feat, feat_plan)
    items: List[Item] = []
    for planned in cat_upserts:
        record = planned.record or {}
        for entry in record.get("features") or ():
            slug = entry.get("slug") or ""
            if not slug or not _is_inline(entry):
                continue
            child = (entry.get("config") or {}).get("type")
            parent = after.get(slug)
            if child and parent and child != parent:
                items.append(Item(ERROR, planned.key, (
                    f"override '{slug}' is '{child}' but its root feature will be "
                    f"'{parent}' after this load — Feature.clean refuses a child "
                    "whose config.type differs from its parent's"
                )))
                break   # the apply stops at the first refusal too
    return items


def dead_end_leaves() -> List[str]:
    """Slugs of ACTIVE, non-deleted leaf categories that type nothing.

    A "leaf" is a category with no active, non-deleted (non-test) children —
    a parent whose children are all retired is what a seller reaches, so it
    counts. "Types nothing" is measured with the library's real resolution
    (``Category.get_all_features``: own + inherited, override-aware), NOT a
    hand-rolled join, so this check can never disagree with the form the
    product would actually render. Such a leaf is a dead end: pickable, and
    producing a listing no form validates and no facet can filter.

    Served two ways: the ``catalog_health`` management command turns a
    non-empty answer into a non-zero exit (a deploy/CI gate), and
    ``load_catalog`` stamps it onto its report so the import that creates
    dead ends says so at import time. ``is_test`` rows are outside canon and
    outside this check on both sides (a scratch row neither is a dead end nor
    shields its parent from being one).
    """
    from .models import Category

    rows = list(
        Category.objects.filter(active=True, deleted=False, is_test=False)
        .only("pk", "slug", "tn_parent_id")
    )
    parents_with_active_child = {
        c.tn_parent_id for c in rows if c.tn_parent_id is not None
    }
    return sorted(
        c.slug
        for c in rows
        if c.pk not in parents_with_active_child
        and not c.get_all_features().exists()
    )


def active_under_inactive_parent() -> List[str]:
    """Slugs of ACTIVE categories whose parent is inactive — resurrections.

    ``active`` is stand-owned curation (``cf.CURATION_KEYS``) and
    ``_apply_category_upsert`` writes it only on CREATE, so a catalogue
    re-import can no longer undo an operator's deactivation. That guard sits
    on one path. A resurrection arriving any other way — a queryset
    ``.update(active=True)``, a fixture applied by an older release, a hand
    edit — leaves nothing for the guard to catch, which is why this finder
    asserts the SHAPE such a write produces rather than the event.

    An operator retires a subtree from the top. Re-activating rows underneath
    a parent that is still off yields a category a seller can reach by search
    or a saved link while the path to it is closed — visible and unreachable
    at the same time, which no deliberate curation produces. A fully retired
    subtree is silent here: the gate names the INCONSISTENT half, so doing it
    right shows nothing.

    Same canon boundary as :func:`dead_end_leaves`: deleted and ``is_test``
    rows are outside the check on both sides of the relationship. Served the
    same two ways — the ``catalog_health`` gate, and ``load_catalog``'s
    post-apply report.
    """
    from .models import Category

    live = Category.objects.filter(deleted=False, is_test=False)
    inactive_pks = set(
        live.filter(active=False).values_list("pk", flat=True)
    )
    if not inactive_pks:
        return []
    return sorted(
        live.filter(active=True, tn_parent_id__in=inactive_pks)
        .values_list("slug", flat=True)
    )


def _flag_db_new_in_canon(cat_plan: List["_Planned"], fix_cat: Dict[str, dict]) -> None:
    """Re-grade db_new rows whose parent the fixture owns (in place).

    ``db_new`` alone says "the catalogue has a row canon never heard of",
    which is a legitimate steady state for a deliberately local subtree. What
    it cannot be legitimate for is a hand row INSIDE an imported subtree: the
    canon almost certainly has a sibling for the same real-world thing under
    a different name, and nothing else in the pipeline will ever say so —
    the row is not in the fixture, so no diff class touches it. One query for
    all db_new keys; the parent is read from the LIVE tree (that is where the
    row actually hangs), the ownership from the fixture keyset.
    """
    from .models import Category

    keys = [p.key for p in cat_plan if p.decision.kind == DB_NEW]
    if not keys:
        return
    parents = {
        slug: parent_slug
        for slug, parent_slug in Category.objects.filter(slug__in=keys).values_list(
            "slug", "tn_parent__slug"
        )
    }
    for p in cat_plan:
        if p.decision.kind != DB_NEW:
            continue
        parent_slug = parents.get(p.key)
        if parent_slug and parent_slug in fix_cat:
            p.decision = Decision("note", DB_NEW_IN_CANON)
            p.note = (
                f"live category '{p.key}' is unknown to the fixture but sits "
                f"under fixture-owned parent '{parent_slug}' — duplicate-shaped: "
                "the imported canon likely offers a sibling for the same thing; "
                "merge into the canon sibling (move listings, then delete) or "
                "move it out of the imported subtree"
            )


def _sibling_name_collisions() -> List[Item]:
    """Live, active, non-deleted siblings sharing a case-folded name.

    Scanned over the whole live tree, because the collision the seller sees
    does not care which side each row came from. ``is_test`` rows are outside
    canon and outside this check for the same reason they are invisible to
    the rest of the loader. Case-folded (``str.casefold``), not search-folded:
    «Другое» vs «другое» is the duplicate a seller sees; «шорты» vs a
    transliteration is a search concern and lives in the search module.
    One report item per (parent, folded name) group, keyed by the parent's
    slug so an operator can find the branch.
    """
    from .models import Category

    rows = Category.objects.filter(
        active=True, deleted=False, is_test=False
    ).values_list("slug", "name", "tn_parent_id")
    parent_slug_by_pk = dict(Category.objects.values_list("pk", "slug"))

    groups: Dict[tuple, List[tuple]] = {}
    for slug, name, parent_id in rows:
        groups.setdefault((parent_id, (name or "").casefold()), []).append((slug, name))

    items: List[Item] = []
    for (parent_id, _), members in sorted(
        groups.items(), key=lambda kv: sorted(m[0] for m in kv[1])
    ):
        if len(members) < 2:
            continue
        members.sort()
        slugs = ", ".join(f"'{slug}'" for slug, _ in members)
        name = members[0][1]
        parent = parent_slug_by_pk.get(parent_id, "(root)") if parent_id else "(root)"
        items.append(Item(
            NAME_COLLISION, parent,
            f"{len(members)} live active siblings share the name "
            f"'{name}': {slugs} — a seller sees one option offered twice; "
            "merge or rename",
        ))
    return items


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


def _item(p: _Planned) -> Item:
    return Item(p.decision.kind, p.key, _detail(p), renamed=p.renamed)


def _apply_phase(report, side, planned, apply_fn):
    for p in planned:
        try:
            with transaction.atomic():  # savepoint: isolate a bad record
                apply_fn(p.record)
            report.add(side, _item(p))
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
        # "error" is a planning-time verdict (an unresolvable source identity):
        # no apply phase ever sees it, so it is reported here.
        if p.decision.op in ("skip", "warn", "note", "touch_base", "drop_base", "error"):
            report.add(side, _item(p))


def _residual_keys(fixture_rec: dict, db_rec: dict) -> List[str]:
    """The record keys that still differ after a successful apply."""
    keys = []
    for key in sorted(set(fixture_rec) | set(db_rec)):
        f_val, d_val = fixture_rec.get(key, _UNSET), db_rec.get(key, _UNSET)
        if f_val == d_val:
            continue
        if key == "features":
            slugs = sorted({
                e.get("slug") or "(slug-less)"
                for e in _entry_diff(f_val if f_val is not _UNSET else [],
                                     d_val if d_val is not _UNSET else [])
            })
            keys.append(f"features[{', '.join(slugs)}]" if slugs else "features")
        else:
            keys.append(key)
    return keys


def _entry_diff(fixture_entries, db_entries) -> list:
    """Feature-list entries present on one side and not the other, verbatim."""
    def norm(entries):
        return [json.dumps(e, sort_keys=True, ensure_ascii=False) for e in entries]

    f_blobs, d_blobs = norm(fixture_entries), norm(db_entries)
    out = [e for e, b in zip(fixture_entries, f_blobs) if b not in d_blobs]
    out += [e for e, b in zip(db_entries, d_blobs) if b not in f_blobs]
    return out


def _report_residuals(report, side, plan, db_after_hashes, db_records, hash_view):
    """Say which reconciled records the DB still cannot spell as the fixture does.

    Called only after a real apply. Not a failure and not a conflict: the write
    the fixture asked for went in, and the sidecar now records both sides, so
    the record converges. What it cannot do is round-trip — an export would
    write something else — and the operator hears that once instead of reading
    "updated" for the same rows on every pass.
    """
    errored = {it.key for it in (report.features if side == "features" else report.categories)
               if it.kind == ERROR}
    for p in plan:
        if not p.decision.reconciled or p.fixture_hash is None or p.key in errored:
            continue  # a record that failed to apply is already reported as one
        db_hash = db_after_hashes.get(p.key)
        if db_hash is None or db_hash == p.fixture_hash:
            continue
        db_rec = db_records.get(p.key)
        fixture_rec = p.record or {}
        if db_rec is None:
            continue
        if hash_view is not None:
            fixture_rec, db_rec = hash_view(fixture_rec), cf.category_sync_view(db_rec)
        keys = _residual_keys(fixture_rec, db_rec)
        report.add(side, Item(RESIDUAL, p.key, (
            "applied, but the live row still differs from the fixture in "
            f"{', '.join(keys) or '(unknown keys)'} — recorded as synced; "
            "run export_catalog to bring canon back in line"
        )))


def _new_base(old_base, plan, db_after_hashes, report, side):
    """Build the new sidecar hashes for one side from the applied outcomes.

    A reconciled key records BOTH halves of what the sync just established:
    the fixture hash it applied and the DB hash that apply produced. Recording
    only the DB half (0.20.1 and older) is what kept a record whose applied DB
    state cannot equal its fixture hash — an override whose label export writes
    conditionally, anything the fixture shape cannot spell — re-planned
    "updated" on every subsequent pass, forever.
    """
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
                result[p.key] = {"fixture": p.fixture_hash, "db": h}
            else:
                # e.g. a fixture that created an is_test row (excluded from the
                # DB view) — nothing to track.
                result.pop(p.key, None)
        # else: untouched (db-only drift, unresolved conflict, plain skip) —
        # keep the old base hash so it stays visible next run.
    return result


def _detail(p: _Planned) -> str:
    dec = p.decision
    if p.note:
        return p.note
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
