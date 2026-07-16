# Changelog

All notable changes to stapel-categories are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [0.4.4] - 2026-07-17

### Changed
- `stapel-core` ceiling raised `>=0.10,<0.11` → `>=0.10,<0.12` (core 0.11
  fleet re-pin: default bus, nav, config-checks, error params/language —
  additive for modules). Suite green against core 0.11.2, no code changes
  needed.

## [0.4.2] - 2026-07-16

### Fixed — dependency pin

- `stapel-core` requirement was still `>=0.8,<0.9` — three releases behind
  every other stapel-* module (`>=0.10,<0.11`, matching stapel-auth /
  stapel-profiles) and behind the 0.10.1 production fix
  (`users_user.avatar` URLField widening). Bumped to `>=0.10,<0.11`. Full
  suite (209 tests) passes unchanged against core 0.10.1 — no code
  changes were needed.

## [0.4.1] - 2026-07-08

L-tier follow-up from the fable review of CAT-1+CAT-2 (the review's verdict
was "ready to ship"; these were the non-blocking residuals). No behavior
change to the review's ratified decisions (db-wins/seed-if-empty/full-table
lock), no schema change — patch bump.

### Fixed
- **Orphan override rows no longer leak.** An override Feature (``tn_parent``
  set) only exists to be linked from a category's materialized feature list;
  when a fixture update drops a category's last reference to one,
  ``load_catalog``'s stale-link cleanup now soft-deletes the now-unreachable
  override instead of leaving a permanently invisible row behind (one leaked
  row per removed override, across repeated fixture edits, before this fix).
  For overrides orphaned by something *other* than ``load_catalog`` (e.g. an
  editor action) — export stays read-only (no delete as a side effect of a
  dump) but now warns on stdout naming them, since they were previously
  dropped from the fixtures with zero visibility.
- **`load_catalog --seed-if-empty` now ignores `is_test` rows when deciding
  whether the DB is "empty".** `is_test` data is outside canon by
  construction (§5); a DB holding only test/scratch rows was previously read
  as "already populated," silently no-op'ing the bootstrap and stranding the
  real canon out of the DB.
- **Confirmed (no code change): duplicate `CategoryFeature.order` values
  cannot make export nondeterministic.** Every sort in the export/load path
  already breaks ties on the row's DB id (`order_by("order", "id")`, in place
  since CAT-1) — a total order, so two consecutive exports of an unchanged
  DB are always byte-identical even when two links share the same `order`.
  Added a direct regression test pinning this invariant (the existing
  duplicate-order test only exercised it indirectly, through a full
  export→load→export round trip).



Catalog fixtures sync, part 2 (CAT-2 of docs/catalog-fixtures-sync.md): the
load side of the reconciliation — a 3-way diff of the committed fixtures
against the live DB, with an honest conflict policy. New management command =
feature → minor bump. No schema changes.

### Added
- **`load_catalog` management command** — reconciles
  `<BASE_DIR>/fixtures/catalog/` (`features.json` / `categories.json` + the
  `.sync-state.json` sidecar) into the live `Category`/`Feature`/
  `CategoryFeature` tables via a **3-way diff**: base = sidecar content-hashes
  (last synced state), theirs = fixture files, ours = live DB (hashed exactly
  as export would serialize it). Per-record classification per the design §4
  table:
  - unchanged → skip (zero writes); fixture-side change with untouched DB →
    fast-forward apply; both sides changed → **conflict, per-record abort by
    default** (the record is left alone, reported, exit code goes non-zero;
    non-conflicting records still apply);
  - changed only in DB since the last export → warn + keep DB ("run
    `export_catalog` first"); present only in DB, never exported → left alone,
    noted as "not in canon";
  - removed from the fixture → **soft-delete by default**
    (`RevisionMixin.soft_delete()`, reversible); removal + concurrent local
    edit → conflict, not a delete.
  - Flags: `--dir DIR`, `--dry-run` (full classification report, zero writes,
    sidecar untouched), `--on-conflict abort|fixture-wins|db-wins` (global
    policy over all conflicts), `--deletions soft|hard|ignore`,
    `--seed-if-empty` (bootstrap idiom: full load on an empty catalog, warn +
    no-op on a populated one — the `load_staff_group_if_empty` precedent).
- All writes go through `Model.save()`/`full_clean()` — never
  `bulk_create`/`QuerySet.update()` (the H-2 lesson): a load earns the same
  side effects as an admin/Studio edit (revision bump, `category.changed`
  outbox emit, `copy_parent_features` on new children, config/slug
  validation). Idempotent by construction: a record whose fixture state
  already equals its DB state is never `save()`d — a second run is zero
  writes, zero revision bumps, zero events (H-3 rule).
- Concurrency: the whole reconciliation runs in one transaction that first
  `select_for_update`-locks the catalog rows in deterministic pk order (the
  M-5 pattern), so a load serializes against concurrent admin/Studio edits.
- `is_test` rows are invisible to the diff (the DB side is built with the
  export serializer, which excludes them): never updated, never deleted —
  a fixture record whose slug collides with a live `is_test` row is a
  per-record error, not a silent overwrite.
- After a successful load the `.sync-state.json` sidecar is rewritten to the
  **applied** state: reconciled keys advance to their new DB hash, deleted
  keys drop out, and untouched keys (DB-only drift, unresolved conflicts)
  keep their old base hash so they stay flagged on the next run.
- Fixture records are normalized to the canonical export shape before
  hashing, so sparse hand-written fixtures (defaulted keys omitted) converge
  instead of re-applying forever; shared override rows (inherit-propagation)
  are copied-on-write when one category's fixture diverges — a load never
  silently rewrites a sibling category's schema through a shared row.
- New engine module `catalog_load.py` (classification, policies, apply,
  report); reuses `catalog_fixtures.py` canonical-JSON/content-hash helpers
  from CAT-1.

### Fixed (fable review of CAT-1 + CAT-2, pre-release)
- **Load-written sidecar no longer poisons export's pre-filter (H).**
  `load_catalog` wrote the post-load `max_revision` into `.sync-state.json`;
  the very next `export_catalog` — including the one the db-only-drift warning
  tells the operator to run — saw an unmoved max(revision) and silently
  skipped, stranding the drift out of canon. The load-written sidecar now
  omits `max_revision` (it is export's pre-filter base, meaningful only for
  export-written sidecars).
- **Export re-parents children of filtered-out categories (H).** A child of a
  soft-deleted or `is_test` category (a state `load_catalog --deletions soft`
  itself produces) exported a dangling `parent_slug`, making the default
  export unloadable on a fresh DB. `parent_slug` now resolves to the nearest
  *exported* ancestor (or `null`), so fixtures are always self-contained.
- **Unreachable fixture states no longer churn revisions forever (H).** Two
  list entries resolving to one row (duplicate bare reference) are a loud
  per-record error; and upserts now carry a dirty guard — if the applicable
  state already equals the DB state (e.g. a hand-written `is_test` inline
  entry, invisible to the export view), nothing is `save()`d: no phantom
  revision bump, no `category.changed` emit on every load (the H-3 rule).
- **`--deletions hard` no longer silently cascades (H).** treenode's
  `delete()` cascades the subtree: hard-deleting a parent category silently
  took down live children the fixture still declared (reported as "skipped"),
  and hard-deleting a root feature cascaded its override rows + links out of
  still-referencing categories. Both now refuse with a per-record error;
  category deletes run children-first so a whole-subtree removal still works.
- `load_catalog` requires `features.json` too (a missing file next to a
  present `categories.json` read as "delete every root feature");
  `export_catalog --include-test` requires an explicit `--out` (an inspection
  dump must never clobber the canonical fixtures + sidecar); a category record
  whose `parent_slug` is itself is a per-record error.
- New regression tests: dirty-state round-trips (root+override of one slug,
  shared/multiple slug-less rows, override chains, soft-deleted links,
  duplicate orders) in `tests/test_catalog_roundtrip_dirty.py`, plus the
  fable-review cases above (198 tests total).

## [0.3.0] - Unreleased

Catalog fixtures sync, part 1 (CAT-1 of docs/catalog-fixtures-sync.md): a
byte-stable export of the live catalog to natural-key JSON fixtures, and a
`is_test` marker so scratch data never ships. New model field + migration →
minor bump.

### Added
- **`is_test` field** on `Category` and `Feature` (`BooleanField`, default
  `False`, indexed). Marks test/scratch rows. Editable in admin with a
  `list_filter`; **not** exposed in the public API serializers or comm
  contracts (`categories.features`) — it is an export-time concern only, not a
  runtime-visibility gate (docs/catalog-fixtures-sync.md §5). Migration
  `0002_category_is_test_feature_is_test` (additive, back-compatible).
- **`export_catalog` management command** — snapshots the live
  `Category`/`Feature`/`CategoryFeature` tables to byte-stable JSON fixtures in
  `<BASE_DIR>/fixtures/catalog/` (the `staff_group` precedent). Natural keys
  (`Category.slug`, root `Feature.slug`, `parent_slug`); each category carries
  its *materialized* ordered feature list (bare `{slug}` reference for a shared
  root feature, inline config for a tree override). Sorted keys, `indent=2`,
  `ensure_ascii=False`, trailing newline — identical DB state yields
  byte-identical files (the `dump_translations`/codegen contract). Writes a
  `.sync-state.json` sidecar (content-hash per natural key + max revision) as
  the 3-way-diff base for a future `load_catalog` (CAT-2).
  - `is_test` rows are excluded **transitively** — a test category or feature,
    and any `CategoryFeature` link touching one, never reach the export.
  - Flags: `--out DIR`, `--dry-run` (report create/update/delete/skip, write
    nothing), `--include-test` (local debug dump only, prints a not-for-commit
    warning), `--force` (ignore the max-revision pre-filter).
- New serialization module `catalog_fixtures.py` (canonical-JSON + content-hash
  helpers, reused by CAT-2).

## [0.2.1] - Unreleased

### Changed
- Pinned `stapel-core` to the `>=0.8,<0.9` window (library-standard §7.1: one
  minor window; floor `0.8.0` is published on PyPI — no pin into the void).
- Pinned `stapel-attributes` to the `>=0.3,<0.4` window (was `>=0.1,<0.2` —
  a stale sibling pin predating attributes 0.3.x; same §7.1 rule).

- CI: added the release-track job (library-standard §7.4) — installs the package
  the way an end user does (`pip install .`, dependencies resolved from PyPI
  strictly by the declared pins, no git-main core, no editable siblings), asserts
  `stapel-core` resolves inside the `0.8` window, and runs an import smoke.
  Advisory (continue-on-error) until the whole stapel graph is on PyPI; becomes
  the blocking precondition for a `vX.Y.Z` tag once it is.

### Packaging
- Tests excluded from the built wheel/sdist (the `stapel_categories.tests`
  subpackage is no longer listed in `[tool.setuptools] packages`). Added
  `[project.urls]`, completed the trove classifiers (MIT/OSI, Python 3.13,
  `Typing :: Typed`, OS Independent, `3 :: Only`, Development Status) and a
  `[tool.ruff]` lint section (single source shared with the git hooks/CI).


## [0.2.0] - Unreleased

Internal code-review fixes to the category feature editor and resolved-schema
resolution. Observable behaviour changes (schema resolution, edit fanout, new
error responses) → minor bump.

### Fixed
- **Resolved-schema dedup is now by slug, not feature id (H-1).**
  `Category.get_all_features()` collapsed an inherited override and its parent
  version into two rows sharing one slug, and the *parent* won downstream
  (`categories.features`, attribute validation) — an `inherit` override applied
  in the admin but did nothing to validation/projections. Dedup is now by slug
  with the version closest to the category winning (self before ancestors,
  nearer ancestor before farther); slug-less rows (headers) still dedup by id.
- **`edit` goes through `Feature.save()`, not `QuerySet.update()` (H-2).** The
  old `.update()` skipped the revision bump, the `category.changed` fanout to
  *every* category carrying the feature, the cached-translation refresh and
  config validation. Edits now re-version the feature and invalidate all
  affected categories; the `icon` field is no longer dropped and an invalid
  config is rejected instead of silently written (L-10).
- **Draft save/clear no longer bumps the category revision or emits
  `category.changed` (H-3, L-8).** The draft is editor scratch state; it is now
  persisted with a column-only `QuerySet.update`, so autosaves don't churn
  revisions and the apply path no longer produces a phantom revision (a bumped
  number that was never persisted, then reused by the next real change).
- **`replace` validates same-tree + existence (L-9)** and **`inherit`
  validates its slug matches the source feature (L-11)** — both bypassed
  `clean()` before and could leave a category with two versions of one root.

### Added
- **Server-side enforcement of `available_actions` (M-4).** `apply` now rejects
  `edit`/`remove` of a slug inherited from the parent (`400`,
  `error.400.categories_feature_editor_invalid`) — the rule the UI already
  showed is now a server boundary.
- **Optimistic concurrency + subtree locking on apply (M-5).** `apply` accepts
  an optional `base_revision`; a mismatch returns `409`
  (`error.409.categories_feature_editor_conflict`), closing the silent
  lost-update ("stale keep-list erases another editor's add"). The category and
  its whole subtree are `select_for_update`-locked at the top of the
  transaction (deterministic pk order) to serialize concurrent applies. The
  feature-editor state now returns `revision` so clients can round-trip it.
- New error keys `error.400.categories_feature_editor_invalid`,
  `error.409.categories_feature_editor_conflict`.

### Changed
- **`categories.features` returns a consistent `(revision, features)` snapshot
  (M-6).** The revision is re-read on both sides of `feature_defs()` and the
  pair is retried until stable, so a concurrent apply can no longer yield a
  torn pair (old revision + new features) that a consumer would cache forever.

### Migration notes
- No schema/migration changes. `apply` clients SHOULD send `base_revision`
  (echoed from the feature-editor state) to opt into the `409` lost-update
  guard; omitting it keeps the old behaviour but only the subtree lock. Any
  client that relied on a draft autosave bumping the category revision must
  stop — draft saves are now revision-neutral.

## [0.1.1] - Unreleased

### Changed
- `Category.save` / `Feature.save` wrap the row write and the post-save
  signal emits in one `stapel_core.comm.mutate_and_emit()` block. Before, a
  bare `save()` in autocommit mode ran the post-save `category.changed`
  emits *after* the row's own transaction (Django fires `post_save` outside
  `save_base`'s atomic context) — the L2 bug shape: a crash between them
  left a committed category with no invalidation event. Now the row,
  `copy_parent_features` side effects and the emit fanout commit as one
  unit.
- `publish_category_changed` now goes through
  `stapel_core.comm.mutate_and_emit()` (stapel-core >= 0.3.3) instead of a
  bare `emit()` — the outbox-atomicity discipline (review C1) is now core
  mechanism: a failed emit sinks the mutating transaction even if the
  caller swallows the exception. `savepoint=False` keeps the Feature-save
  N-fanout free of per-emit savepoints. Core pin bumped to `>=0.3.3,<0.4`.
- CI/pre-commit now run the `emit-check` static gate
  (`python -m stapel_core.lint.emit_check .`) next to ruff.
- Tests: the failing-emit rollback test fails emit at the delivery seam
  (`stapel_core.comm.actions.deliver`); new adversarial test — a swallowed
  emit failure still cannot commit the row.

## [0.1.0] - Unreleased

Initial release. Ported from the legacy catalog's `categories` app.

### Added
- **Category tree** (django-treenode): name/slug/comment/draft/icons/active/
  translatable, revision-synced (`RevisionMixin`), soft-delete.
- **Feature tree** with a polymorphic `config` JSONField, feature inheritance
  (`get_all_features` walks self + ancestors), and the `copy_parent_features`
  post-save signal.
- **CategoryFeature** through-model with explicit per-category ordering.
- **Feature editor**: keep/add/edit/inherit/remove/create/replace action model
  with descendant propagation, draft→apply lifecycle (draft is API state),
  children CRUD/reorder/undelete, convert-type (select↔string).
- **HTTP API**: Category & Feature list/retrieve (revision-sync pagination),
  carousel, `/features`, `/children`, bulk-commands, feature-editor draft/apply,
  validate-dto / validate-configs. Staff vs public vs service permissions.
- **comm surface**:
  - Function `categories.features(category_id)` returns the resolved feature
    schema (own + inherited, config merged with type defaults), cacheable by
    category `revision` — so stapel-listings validates values without importing
    this module. JSON schema in `schemas/functions/`.
  - Action `category.changed` emitted on category/feature mutation for
    downstream cache invalidation. JSON schema in `schemas/emits/`.

### Dependencies
- Delegates **all** attribute config/value validation, the type registry, the
  polymorphic serializers and the admin config-editor widget to
  **stapel-attributes** (`>=0.1,<0.2`) — `feature_types` is NOT re-implemented
  here.

### Fixed (while porting)
- **Latent `Category.Meta` shadowing bug**: the source had a second
  `class Meta` that silently shadowed the first, dropping the `revision` index
  and other options. Merged into one Meta; both the `revision` index and
  `verbose_name_plural` now apply (regression test + migration assert it).
- Deduplicated the `_get_feature_slug` / `_build_feature_lookup` helpers that
  belong to the attribute engine — imported from stapel-attributes.
- `categories.features` now carries `show_at_title`, `show_as_badge` and
  `translate` per feature (listings integration): stapel-attributes'
  `dto_to_dao` reads these off the FeatureDef to build the title/badge
  projections, so omitting them produced empty `features_title` /
  `features_badges` downstream. Additive to the payload and to the
  `ResolvedFeature` shape documented in `schemas/functions/categories.features.json`.
- `publish_category_changed` no longer swallows `emit` failures (review C1):
  the emit runs inside the mutating `save()`'s atomic block, so a delivery
  failure now rolls the mutation back instead of committing a row with no
  `category.changed` event — a lost invalidation would strand every downstream
  `categories.features` cache. Covered by an atomicity test.

### Decoupling
- Dropped organization/scope and marketplace coupling; the module is generic.
- CDN icons are opaque string references/UIDs — **no** dependency on stapel-cdn.
- Translation-key display goes through the `DISPLAY_TRANSLATOR` seam (identity
  default); the module stores keys, it does not own a catalog.

### Not ported
- `feature_types/` engine and its ~3.4k-line test suite (owned by
  stapel-attributes).
- Legacy-catalog-specific seeds (`categories.json`, `load_categories`,
  `prefill_catalog_assets`) — app-layer concerns.
