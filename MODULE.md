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
- **The disclosure axis** `Feature.visibility` — `public` (default) / `owner`
  / `staff`: which audience may READ a stored value, for attributes that
  *identify* a specific unit (VIN, IMEI, serial, registry number) rather than
  describe it. Orthogonal to `mandatory` — a non-public feature is still
  required, validated, stored and moderated. This module only RECORDS the
  decision and carries it across every boundary (`feature_defs()`, the
  serializers, the editor, the fixtures, `categories.features`); the hiding
  itself is stapel-listings', off the stamp the attribute engine writes into
  each value. The one behaviour owned here: a non-public feature is never a
  title and never a badge — `clean()` and `save()` both force the two flags
  off, so no row can claim otherwise. Changing the axis on a live catalogue is
  not complete until `listings_reproject_features` re-stamps stored values.
- **The composite `group`** (stapel-attributes 0.6.0) is the one feature type
  whose `config` carries other feature definitions: `config.fields` is a list
  of full FeatureDefs and the value is a list of rows keyed by child slug.
  Nothing here special-cases it — `config` is stored verbatim and validated by
  the engine, which enforces the boundaries (nesting depth 1, no `header`
  child, no `rules` on a child) and reports them on the `config` field. Two
  places DO have to know: `translation_keys` walks a group's children (they are
  not catalog rows, so nothing else reaches their names and option labels), and
  `docs/schema.json` names `GroupConfig`/`GroupDto` in both discriminators.
  Both are gated — `tests/test_feature_group_kind.py` for the crossings,
  `tests/test_resolved_feature_contract.py` for the shape.
- The **feature editor**: a keep/add/edit/inherit/remove/create/replace action
  model with descendant propagation and a draft→apply lifecycle (draft is API
  state, not a textarea). Plus children CRUD/reorder/undelete and convert-type
  (select↔string).
- A revision-sync **HTTP API** for Category & Feature (list/retrieve, carousel,
  `/features`, `/children`, bulk-commands, feature-editor draft/apply,
  validate-dto / validate-configs). The public category payload is a **frozen
  key set** (`tests/test_public_read.py::PUBLIC_CATEGORY_KEYS`): every
  anonymous read serves the same projection, and `external_id` /
  `external_source` — the source catalogue's own node ids — are NOT in it
  (0.13.0). Provenance is an operator fact: it stays on the Django admin, the
  staff bulk serializer and the staff write serializer
  (`CategoryStaffSerializer`, served on create/update only).
- **Three public tree reads that share one visibility rule** (`roots`,
  `{id}/children`, `by-slug/{slug}`). `roots` returns the top-level
  categories and `by-slug` resolves a storefront URL segment to one category;
  before 0.12.0 neither existed, so a client that wanted either had to list
  the whole table and filter it client-side. All three go through
  `views.visible_categories()` — one definition, so they cannot drift into
  showing different catalogues — and all three carry
  `Cache-Control: public, max-age=TREE_CACHE_TIMEOUT`. `roots` also caches
  server-side under a key fingerprinted by the tree's revision state (the
  `categories.suggest` mechanism), so an edit retires the entry immediately
  instead of waiting out a TTL.
- A **comm surface**: Functions `categories.features` (resolved schema for a
  category), `categories.path` (root->leaf ancestry for a batch of categories),
  `categories.names` (batch of ids -> display name + slug) and
  `categories.suggest` (category NAMES matched for a type-ahead, answered
  with their ancestry), plus emitted Action `category.changed`.
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
| `TREE_CACHE_TIMEOUT` | `300` | value | Seconds the public tree reads (`roots`, `{id}/children`, `by-slug/{slug}`) are cacheable for, and the ceiling on the server-side `roots` entry. The storefront's cold path — the first thing every visitor asks for and the last thing that changes. |
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
| Function (provides) | `categories.names` | `{ids:[id,...]}` -> `{names: {"<id>": {name, slug}}}` | `schemas/functions/categories.names.json` |
| Function (provides) | `categories.suggest` | `{terms:[folded,...], limit}` -> `{categories:[{id, slug, name, path, path_ids, depth, match}]}` | `schemas/functions/categories.suggest.json` |
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

`categories.names` is the caption counterpart: a batch of bare category ids
in, `{"<id>": {name, slug}}` out — keys as strings on both sides of the wire
(the `categories.path` rule), names rendered through the `DISPLAY_TRANSLATOR`
seam exactly as `categories.suggest` renders them. It exists because a
consumer holding path IDS (stapel-search's goods-driven suggest rows) had no
fleet Function that answers "what is 163 called?" — `path` answers id-paths
and `suggest` answers terms — and re-deriving names from a projection is the
seam defect the comm surface exists to prevent. Deleted rows and unknown ids
are absent from the mapping (stale ids degrade to no caption, not an error);
inactive rows still answer, because a listing can sit in a category retired
after publication. The batch is schema-capped at 200.

`categories.suggest` matches category names for a type-ahead. «Шорты» is not
one category — it is a leaf under men's, under women's and under children's
clothing, and the ancestor path is the only thing that tells the three apart
in a dropdown — so `path` (display names) and `path_ids` (the same ancestry as
ids) travel together in one answer: a consumer needs the first to render a row
and the second to navigate it, and deriving one from the other outside this
module means a second call and a second chance to disagree with the tree.

Visibility is **inherited**: `active=False`, `is_test` and soft-deleted rows
are excluded, and so is a live leaf hanging under a retired ancestor, because
it is not reachable in the catalogue and offering it navigates a buyer into a
page that is not there.

What it deliberately does not own is the query language. Terms arrive already
folded and already expanded — synonyms, transliteration — by whoever asked;
a second normalizer here would be a second answer to "what did the user mean",
and the declared consumer (stapel-search 0.7's `CATEGORY_SUGGEST_FUNCTION`)
has exactly one. `match: prefix | substring` is reported and not ranked on:
the caller ranks by live listing count, and only the caller has that number.

Matching happens in Python over a folded name index built from ONE read of
the tree and cached under a fingerprint of the tree's revision state, so a
mutation retires it at once and an unchanged tree costs a single cheap
aggregate. It is not SQL because `LOWER()` is ASCII-only on SQLite: a database
case function would answer «Шорты» to a Postgres deployment and nothing to a
SQLite one, which is the class of divergence that makes a test suite agree
with a stand that is wrong. `functions.fold` is the wire normal form the
schema documents — `ё` folds into `е` because users type both, Cyrillic
diacritics are kept because NFD would merge «мой» into «мои».

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

The fixture's contract is **taxonomy + features; presentation is the
operator's** (0.13.0). `catalog_icon` / `carousel_icon` / `carousel_enabled`
still travel in `categories.json` (an export→restore of a whole stand keeps
its curation) but are excluded from the 3-way content hash on every side
(`catalog_fixtures.category_sync_view`, sidecar `STATE_VERSION` 3), and the
loader writes them **only when it creates the row** — a catalogue re-import
can never reset a curated carousel again. `tn_priority` was already
fixture-invisible; these three now follow the same ownership.

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
rewritten to the applied state. The report also carries three
duplicate/dead-end diagnostics (0.13.0): a db-only row whose PARENT the
fixture owns is re-graded from the generic `db_new` to `db_new_in_canon`
(duplicate-shaped — a hand row parked between imported canon siblings),
`name_collision` warns when two live, active siblings share a case-folded
name (what a seller sees as one option offered twice), and
`report.dead_end_leaves` lists the active leaves that type nothing after the
load (the `catalog_health` gate's finding, echoed at import time). All three
warn without failing the load.

`python manage.py catalog_health` is the standing gate on dead ends: it lists
every ACTIVE, non-deleted leaf category with zero features (own + inherited,
resolved with `Category.get_all_features` — the same logic the product
renders with) and exits non-zero when any exist. No escape flag: attach a
feature, deactivate the leaf, or merge it. A stale-link removal that leaves an override
row (`tn_parent` set) linked to zero categories soft-deletes it — an override
has no natural key of its own, so an unreferenced one would otherwise be
invisible to every future export/load and leak forever; `export_catalog`
separately warns (stdout only, no write) if it finds one left behind by
something other than `load_catalog` (e.g. an editor action).

- **Natural keys, not pks.** `Category.slug` (globally unique) and root
  `Feature.slug` (unique among roots). A category feature list entry is either
  a bare `{"slug": …}` reference to a shared root feature, or an inline
  override (`{"slug", "config", "mandatory", "show_as_badge", "show_at_title",
  "visibility", "translate", "rules", "description", "example", "default",
  "hints", "group"}`) when the linked row is a tree override (`tn_parent`
  set).
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
