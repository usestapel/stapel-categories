# Changelog

All notable changes to stapel-categories are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

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

Initial release. Ported from `legacy-catalog`'s `categories` app.

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
- legacy-specific seeds (`categories.json`, `load_categories`,
  `prefill_catalog_assets`) — app-layer concerns.
