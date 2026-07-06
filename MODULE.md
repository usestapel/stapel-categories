# stapel-categories — MODULE.md

> Agent-facing map of this module: what it provides, where to extend it
> without forking, and what not to do. Kept in the same PR as any change
> to a seam. See also README.md and CHANGELOG.md.

## What this module provides

- A hierarchical **Category** tree (django-treenode) with revision-synced,
  soft-deletable nodes, opaque CDN icon references, and an ordered M2M to
  Features through `CategoryFeature(order)`.
- A parallel **Feature** tree whose typed `config` JSONField is validated by
  **stapel-attributes**. Feature inheritance walks self + ancestors
  (`Category.get_all_features`); `copy_parent_features` seeds a new child from
  its parent.
- The **feature editor**: a keep/add/edit/inherit/remove/create/replace action
  model with descendant propagation and a draft→apply lifecycle (draft is API
  state, not a textarea). Plus children CRUD/reorder/undelete and convert-type
  (select↔string).
- A revision-sync **HTTP API** for Category & Feature (list/retrieve, carousel,
  `/features`, `/children`, bulk-commands, feature-editor draft/apply,
  validate-dto / validate-configs).
- A **comm surface**: Function `categories.features` (resolved schema for a
  category) and emitted Action `category.changed`.
- **Catalog fixtures export** (`export_catalog` management command): a
  byte-stable, natural-key JSON snapshot of the live catalog for reconciliation
  with a host project's `fixtures/catalog/` — see below.

### Ownership boundary with stapel-attributes

This module owns the **tree, inheritance, ordering and editor lifecycle**. The
**attribute engine** — the feature-type registry, per-type Config/DTO/DAO
classes, config/value validation (`validate_feature_config`,
`validate_dto_structured`, `validate_configs_structured`), polymorphic
serializers, and the schema-driven admin config-editor widget — lives in
**stapel-attributes** and is imported. Do not re-add a `feature_types` module
here; register new attribute types in stapel-attributes (its `EXTRA_TYPES`
registry), not here.

## Extension points (fork-free)

### Settings — `STAPEL_CATEGORIES` namespace (`conf.py`)

Resolution order per key: `settings.STAPEL_CATEGORIES[key]` -> flat Django
setting of the same name -> environment variable -> default. Read lazily.

| Key | Default | Semantics | What it customizes |
|---|---|---|---|
| `CAROUSEL_CACHE_TIMEOUT` | `300` | value | Seconds the `carousel` action caches its response. |
| `FEATURE_DISPLAY_CACHE_TIMEOUT` | `60` | value | Seconds an admin feature display label is memoized. |
| `DISPLAY_TRANSLATOR` | `stapel_categories.translation.identity_translator` | **REPLACE** (dotted path, single strategy) | Callable `(key: str) -> str` that renders a translation key for `__str__`/admin display. Default is identity — the module stores keys, not resolved text. Point it at a translation backend (e.g. a wrapper over the `translate.resolve` comm Function) to show resolved names. |

There are no open (merge) registries in this module — the one registry that
matters, the feature-type registry, is owned by stapel-attributes.

### Serializer seams (`views.py`)

Both viewsets are DRF `ModelViewSet`s; swap serializers by subclassing and
overriding `serializer_class` / `get_serializer_class`, then remount the URL.

| ViewSet | Default serializers |
|---|---|
| `CategoryViewSet` | `CategorySerializer` (+ `CategoryBulkSerializer`, `CategoryBulkCommandSerializer`, feature-editor serializers) |
| `FeatureViewSet` | `FeatureCompactSerializer` (list) / `FeatureCreateUpdateSerializer` (write) / `FeatureSerializer` (detail) |

### Feature-editor extension points (`feature_editor.py`)

The editor is a pure function over `FeatureEditorItem`s
(`apply_feature_editor_changes(category, items, base_revision=None)`), separate
from the HTTP layer — call it directly from a management command or a host
workflow. The action set (`keep/add/edit/inherit/remove/create/replace`) and its
descendant-propagation rules are the module contract; adding an action is an
upstream change (it also needs an editor-serializer choice + a UI action in the
attributes-based front end).

**Invariants enforced server-side** (not just in the UI): `edit`/`remove` are
rejected for a slug inherited from the parent (raise `FeatureEditorError`);
`inherit` must keep its source feature's slug; `replace` only swaps another
version from the same feature tree; `edit` runs through `Feature.save()` +
`clean()` so it re-versions the feature, fans `category.changed` out to every
category carrying it, and validates the config. Resolved-schema dedup is by
**slug** (nearest version wins), so an `inherit` override actually takes effect
downstream.

**Concurrency**: `apply` `select_for_update`-locks the category and its whole
subtree (deterministic pk order) up front. Pass `base_revision` (echoed from the
feature-editor state's `revision`) for an optimistic-concurrency check — a
mismatch raises `FeatureEditorConflict` (HTTP `409`), closing the lost-update
where a stale editor's keep-list erases a concurrent add. The draft is editor
scratch state: it is persisted column-only (no revision bump, no
`category.changed`), so autosaves and the post-apply draft clear are
revision-neutral.

### Admin UI

The Feature `config` field renders through stapel-attributes'
`ConfigEditorWidget`, resolved via `get_config_editor_widget("config")` so a
host can swap it with the attributes `ADMIN_WIDGETS` seam. Restyling, locales
and extra assets are attributes' seams (`ADMIN_EXTRA_CSS/JS`, `ADMIN_LOCALES`)
— see stapel-attributes MODULE.md. The feature-editor / children-editor screens
consume attributes' Lit components; this repo owns only their server side.

### comm surface

| Kind | Name | Payload | Schema |
|---|---|---|---|
| Function (provides) | `categories.features` | `{category_id}` -> `{category_id, revision, features:[{id,slug,name,mandatory,config}]}` | `schemas/functions/categories.features.json` |
| Action (emits) | `category.changed` | `{category_id, revision}` | `schemas/emits/category.changed.json` |

`category.changed` is emitted from post-save signals on Category (and per
affected category on Feature save) so consumers invalidate any cached
`categories.features` result. The `categories.features` payload is a consistent
`(revision, features)` snapshot: the revision is re-read on both sides of the
feature resolution and retried until stable, so a concurrent apply never yields
a torn pair (old revision + new features) a consumer would cache under the wrong
revision. Emission goes through the transactional
outbox; `Category.save` / `Feature.save` wrap the row write and the signal
emits in one `stapel_core.comm.mutate_and_emit()` block, so the row and its
invalidation events commit together or not at all.

## Catalog fixtures (`export_catalog`)

`python manage.py export_catalog` writes the live catalog to byte-stable JSON
in `<BASE_DIR>/fixtures/catalog/` (override with `--out DIR`): `features.json`
(root feature definitions, keyed by `slug`), `categories.json` (tree edges via
`parent_slug` + each category's *materialized* ordered feature list), and a
`.sync-state.json` sidecar (content-hash per natural key + max revision) that a
future `load_catalog` (CAT-2) uses as its 3-way-diff base. Design:
`docs/catalog-fixtures-sync.md`.

- **Natural keys, not pks.** `Category.slug` (globally unique) and root
  `Feature.slug` (unique among roots). A category feature list entry is either
  a bare `{"slug": …}` reference to a shared root feature, or an inline
  override (`{"slug", "config", "mandatory", "show_as_badge", "show_at_title",
  "translate"}`) when the linked row is a tree override (`tn_parent` set).
  Override rows get **no** invented natural key — every referencing category
  inlines its config independently (no dedup/owner heuristic; §2).
- **`is_test` is an export filter, transitively.** A test category or feature,
  and any `CategoryFeature` link touching one, are excluded. `is_test` is
  admin-editable and filterable but is **not** in the public API serializers or
  the `categories.features` contract — do not add it there; it is not a
  runtime-visibility gate (§5).
- **Byte-stable.** Sorted keys, `indent=2`, `ensure_ascii=False`, trailing
  newline; no timestamps/UUIDs in bodies (provenance lives in the git commit).
  Identical DB state ⇒ byte-identical files — the same contract as
  `dump_translations` / codegen artifacts.
- Flags: `--dry-run` (report, write nothing), `--include-test` (local debug
  dump only — not for commit), `--force` (ignore the revision pre-filter).
- The canonical-JSON + content-hash helpers live in `catalog_fixtures.py`
  (shared with CAT-2's loader). Do not fork a second byte-stable dumper.

## Anti-patterns

- **Don't re-implement attribute validation or types** — import from
  stapel-attributes. A `feature_types` module here is a bug.
- **Don't fork to change behavior** — every knob above is a seam.
- **Don't import other stapel modules** — cross-module communication is comm
  (Actions/Functions) by string name only. `categories.features` exists so
  listings never imports this package.
- **Don't reintroduce a second `class Meta`** on a model — it silently shadows
  the first (the exact bug fixed in 0.1.0).
- **Don't bypass the settings namespace** with `os.getenv` at import time.
- **Don't leak `is_test` into runtime read paths** — it is an `export_catalog`
  filter (and an admin marker), not a visibility gate. Keep it out of the
  public serializers and the `categories.features` contract.
- **Don't emit outside the mutation's transaction, and never swallow an emit
  failure** — a committed category without its `category.changed` event
  strands every downstream `categories.features` cache. Mutation+emit go
  through `stapel_core.comm.mutate_and_emit()`; CI and the git hooks gate
  this with `python -m stapel_core.lint.emit_check .`.

## App-layer override vs upstream contribution — rule of thumb

**App-layer** (host project, no fork) if the change fits a seam above: a
settings key, a viewset subclass + URL remount, a `category.changed` subscriber,
a new attribute type registered in stapel-attributes, a custom
`DISPLAY_TRANSLATOR`.

**Upstream contribution** if it needs new model fields/migrations, a new
endpoint, a new settings key/seam, a new feature-editor action, or a changed
committed schema.

Litmus test: if you'd have to monkeypatch or edit code inside
`stapel_categories/` — it's upstream. If a setting, subclass, receiver or comm
call gets you there — it's app-layer.
