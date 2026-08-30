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
- **Conditional rules and form metadata** on each Feature, siblings of the
  flags rather than parts of `config`: `rules` (stapel-attributes' closed
  grammar — require/show/hide/forbid_option/limit), plus `description`,
  `example`, `default`, `hints` and `group`. `Feature.clean()` parses the rule
  set and checks the hint shape; `validators.feature_warnings(category)`
  reports (never raises) a rule condition or `optionsRef.parentFeature` naming
  a slug the category does not define — the only question the *whole* resolved
  set can answer.
- The **feature editor**: a keep/add/edit/inherit/remove/create/replace action
  model with descendant propagation and a draft→apply lifecycle (draft is API
  state, not a textarea). Plus children CRUD/reorder/undelete and convert-type
  (select↔string).
- A revision-sync **HTTP API** for Category & Feature (list/retrieve, carousel,
  `/features`, `/children`, bulk-commands, feature-editor draft/apply,
  validate-dto / validate-configs).
- A **comm surface**: Functions `categories.features` (resolved schema for a
  category) and `categories.path` (root->leaf ancestry for a batch of
  categories), plus emitted Action `category.changed`.
- **Catalog fixtures sync** (`export_catalog` / `load_catalog` management
  commands): a byte-stable, natural-key JSON snapshot of the live catalog in a
  host project's `fixtures/catalog/`, reconciled back into a DB via a 3-way
  diff with an honest conflict policy — see below.

### Ownership boundary with stapel-attributes

This module owns the **tree, inheritance, ordering and editor lifecycle**. The
**attribute engine** — the feature-type registry, per-type Config/DTO/DAO
classes, config/value validation (`validate_feature_config`,
`validate_dto_structured`, `validate_configs_structured`), polymorphic
serializers, and the schema-driven admin config-editor widget — lives in
**stapel-attributes** and is imported. Do not re-add a `feature_types` module
here; register new attribute types in stapel-attributes (its `EXTRA_TYPES`
registry), not here.

The **shape** of a feature definition is likewise upstream's:
`stapel-attributes/docs/feature-def.schema.json` is the canon (§68 — one JSON,
a fan of emitters). `schemas/functions/categories.features.json`'s
`$defs.ResolvedFeature` is one of those emitters and is gated against it by
`tests/test_resolved_feature_contract.py`: every canon property except `config`
must be present and required here. The canon is read from a sibling checkout
when the workspace has one and from the installed package otherwise (it is
package data), so the gate never degrades to a skip. When upstream adds a
`FeatureDef` field, that test fails until this module carries it — through
`feature_defs()`, the serializers, the editor, the fixtures and the admin.

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
(`apply_feature_editor_changes(category, items, base_revision)`), separate
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
subtree (deterministic pk order) up front. `base_revision` (echoed from the
feature-editor state's `revision`) is required and checked optimistically — a
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

### Admin categories — `@access` declarations (admin-suite AS-5)

Every model in `models.py` carries (or implicitly defaults to) a
`stapel_core.access.access` category — one declaration, consumed by admin
visibility, default staff rights, and the audit report (admin-suite §0).
Undecorated = `business` (visible, staff-manageable) and is the correct,
zero-effort default for domain tables.

All three models here are `business` and stay undecorated — none fit `ops`
(outbox/dedup/audit-log/TTL-junk machinery) or `secret` (token/key/credential
carriers):

- `Category` — the module's core taxonomy table; the admin-suite doc's own
  verbatim `business` example. `RevisionMixin` only adds `revision`
  (monotonic sync counter) and `deleted` (soft-delete flag) fields directly
  on the model — it does not introduce a separate revision-log/audit-trail
  model, so there is no shadow `ops`-shaped table to classify here.
- `Feature` — the parallel attribute-definition tree (name, typed `config`,
  display flags). Same `RevisionMixin` fields, same reasoning; it holds
  catalog metadata, not credentials or machinery journals.
- `CategoryFeature` — a plain through table (`category`, `feature`, `order`)
  recording M2M ordering. No timestamps, no delivery/dedup semantics, no
  sensitive fields — an ordinary junction row, not an `ops` journal.

No decorator changes were made and `admin.py` (`CategoryAdmin`,
`FeatureAdmin`) is untouched — there is no ops/secret model here to route
through `StapelModelAdmin`.

### comm surface

| Kind | Name | Payload | Schema |
|---|---|---|---|
| Function (provides) | `categories.features` | `{category_id}` -> `{category_id, revision, features:[ResolvedFeature]}` | `schemas/functions/categories.features.json` |
| Function (provides) | `categories.path` | `{category_ids:[id,...]}` -> `{"<id>": ["<root_id>",…,"<id>"]}` | `schemas/functions/categories.path.json` |
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

`categories.path` answers ancestry from django-treenode's denormalized
`tn_ancestors_pks` — one query for a whole batch, no tree walk and no second
hierarchy of our own. Its segments are category **ids**, not slugs: the
declared consumer (stapel-search's `CATEGORY_PATH_FUNCTION`, canonical name
`categories.path`) feeds the last segment of a requested path straight back
into `categories.features`, whose payload is typed as an integer id. An id
with no row is absent from the answer rather than mapped to an empty path, so
"no such category" stays distinguishable from "a root category" (whose path is
one element long).

## Catalog fixtures (`export_catalog` / `load_catalog`)

`python manage.py export_catalog` writes the live catalog to byte-stable JSON
in `<BASE_DIR>/fixtures/catalog/` (override with `--out DIR`): `features.json`
(root feature definitions, keyed by `slug`), `categories.json` (tree edges via
`parent_slug` + each category's *materialized* ordered feature list), and a
`.sync-state.json` sidecar (content-hash per natural key + `max_revision`,
export's pre-filter base — the sidecar a *load* rewrites deliberately omits
`max_revision` so a post-load export never falsely skips) that `load_catalog`
uses as its 3-way-diff base. `parent_slug` always references a record in the
same file: children of filtered-out parents (soft-deleted / `is_test`) are
re-parented to the nearest exported ancestor. Design:
`docs/catalog-fixtures-sync.md`.

`python manage.py load_catalog` reconciles those fixtures back into the DB:
base = sidecar hashes, theirs = files, ours = live DB. Fast-forwards apply;
both-sides-changed records **abort per-record by default** (report + non-zero
exit; override with `--on-conflict fixture-wins|db-wins`); removals from the
fixture **soft-delete** by default (`--deletions hard|ignore` to change; hard
refuses per-record when the treenode/FK cascade would silently take down live
children or still-linked categories); DB-only drift warns and is kept. All writes go through `save()`/`full_clean()`
(never bulk/`.update()` — H-2), under a `select_for_update` catalog lock (M-5),
and a re-run on materialized fixtures is zero saves / zero events. Engine:
`catalog_load.py`. `--seed-if-empty` is the bootstrap idiom (full load on an
empty catalog, no-op otherwise — "empty" ignores `is_test` rows: a DB holding
only test/scratch data still seeds the canon); `--dry-run` prints the full
classification without writing. After a successful load the sidecar is
rewritten to the applied state. A stale-link removal that leaves an override
row (`tn_parent` set) linked to zero categories soft-deletes it — an override
has no natural key of its own, so an unreferenced one would otherwise be
invisible to every future export/load and leak forever; `export_catalog`
separately warns (stdout only, no write) if it finds one left behind by
something other than `load_catalog` (e.g. an editor action).

- **Natural keys, not pks.** `Category.slug` (globally unique) and root
  `Feature.slug` (unique among roots). A category feature list entry is either
  a bare `{"slug": …}` reference to a shared root feature, or an inline
  override (`{"slug", "config", "mandatory", "show_as_badge", "show_at_title",
  "translate", "rules", "description", "example", "default", "hints",
  "group"}`) when the linked row is a tree override (`tn_parent` set).
  Category records carry `external_id` + `external_source` (the id in the
  catalogue they were imported from, and which catalogue that is) — *not* a
  natural key: the fixtures still address categories by slug. It IS the
  **re-import identity**, see below.
  Override rows get **no** invented natural key — every referencing category
  inlines its config independently (no dedup/owner heuristic; §2).
- **Re-imports key on source identity, not on the slug.** An imported
  category's slug is derived from the source catalogue's node path, so the
  source renaming a node moves the slug while the node stays the same node.
  `load_catalog` therefore resolves a fixture row to a live row by
  `(external_source, external_id)` **first**, and only falls back to `slug`
  for rows carrying no external id (hand-seeded ones, which have no other
  key). A matched row is updated in place — name, parent, features, flags and
  the slug, which is what the rename *is* — so a re-sync never leaves a
  duplicate beside the row that holds the listings. The pair, not the bare id:
  two source catalogues numbering from 1 would otherwise collapse onto each
  other's rows; `external_source` is blank for a single-source catalog and for
  every row written before the field existed, so it degrades to plain
  `external_id` matching there. The plan (`--dry-run`) reports a rename as
  `» slug 'a' → 'b' (external_id 'X')` — an update, never an add + a remove —
  and refuses, loudly and per record, three things it must not guess: two live
  rows claiming one source id, a rename whose target slug is held by a row
  this import does not move (identity wins the match, so the *rename* is what
  gives way — the other row is never clobbered), and a cycle of renames. A
  chain (`a→b` while `b→c`) is not a refusal: the holder is sequenced first
  and both land in one run.
- **`is_test` is an export filter, transitively.** A test category or feature,
  and any `CategoryFeature` link touching one, are excluded. `is_test` is
  admin-editable and filterable but is **not** in the public API serializers or
  the `categories.features` contract — do not add it there; it is not a
  runtime-visibility gate (§5).
- **Byte-stable.** Sorted keys, `indent=2`, `ensure_ascii=False`, trailing
  newline; no timestamps/UUIDs in bodies (provenance lives in the git commit).
  Identical DB state ⇒ byte-identical files — the same contract as
  `dump_translations` / codegen artifacts.
- **The sidecar carries a version** (`catalog_fixtures.STATE_VERSION`, 2 since
  0.7.0). Bump it whenever a stored content-hash stops meaning what it meant —
  a record gaining a key invalidates every hash a previous export wrote, and a
  loader reading that base without the bump would classify the whole catalog as
  conflicted instead of saying so.
- Flags: `--dry-run` (report, write nothing), `--include-test` (local debug
  dump only — requires an explicit `--out`, never clobbers the canonical
  fixtures), `--force` (ignore the revision pre-filter).
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
