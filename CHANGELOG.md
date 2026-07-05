# Changelog

All notable changes to stapel-categories are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

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
