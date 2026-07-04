# Changelog

All notable changes to stapel-categories are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

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
