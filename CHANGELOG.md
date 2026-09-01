# Changelog

## [0.9.1] — 2026-09-01

Patch (pre-1.0: minor = breaking, patch = compatible). Comments, docs and test
data only — no model, migration, comm-surface or fixture-format change.

### Changed

- **Import prose is source-neutral.** The comments and docstrings that
  explained `external_id` / `external_source`, the slug-derivation note in
  `catalog_load`, the tree-size note in `functions`, the re-sync plan line in
  `load_catalog` and the composite-kind note in the tests named the external
  marketplace whose catalogue was imported. They now say what they mean — an
  imported external catalogue — which is also the only thing this module knows
  about it: `(external_source, external_id)` is an opaque pair here and always
  was.
- **Test data is neutral and English.** `SourceIdentityTests` seeds
  `catalog-a` / `catalog-b` sources and phones/used-phones/mobile-phones
  categories instead of two named marketplaces and transliterated Russian
  slugs. The scenarios, assertions and coverage are unchanged.

## [0.9.0] — 2026-08-31

### Added — `categories.suggest`: names in, nodes with their ancestry out

The counterpart of `categories.path` for the other direction, and it exists
here for the same reason: names, ancestry and the retired/test/soft-deleted
state of every node are this module's, and a consumer re-deriving any of them
from a projection is the seam defect the comm surface exists to prevent.

```python
call("categories.suggest", {"terms": ["шорты", "shorty"], "limit": 50})
# -> {"categories": [
#      {"id": 101, "slug": "muzhskaya-odezhda-shorty", "name": "Шорты",
#       "path": ["Одежда", "Мужская одежда", "Шорты"],
#       "path_ids": ["46", "48", "101"], "depth": 3, "match": "prefix"},
#      … ]}
```

stapel-search 0.7.0 is the consumer: its type-ahead offers destinations, and
«шорты» is a leaf under men's, women's and children's clothing at once — the
ancestor path is the only thing that tells the three apart.

- **`path` and `path_ids` travel together.** A dropdown row needs the first to
  render and the second to navigate, and deriving one from the other outside
  this module means a second call and a second chance to disagree with the
  tree.
- **Visibility is inherited.** `active=False`, `is_test` and soft-deleted are
  excluded — and so is a live leaf under a retired ancestor, because it is not
  reachable in the catalogue and offering it navigates a buyer into a page
  that is not there.
- **It does not own the query language.** Terms arrive already folded and
  already expanded (synonyms, transliteration) by whoever asked. A second
  normalizer here would be a second answer to "what did the user mean", and
  there is exactly one of those, in the search module.
- **`match: prefix | substring`** is reported and not ranked on. The caller
  ranks by live listing count, and only the caller has that number.
- **At most two queries, one of them only on a cold cache.** The folded name
  index is built from ONE read of the tree and held under a fingerprint of the
  tree's own revision state — `(max revision, row count)` — so a mutation
  retires it immediately and an unchanged tree costs a single cheap aggregate.
  `test_query_count_does_not_grow_with_the_answer` and
  `test_an_unchanged_tree_costs_one_query` pin both halves.

Matching happens in Python, not in SQL, and that is not a shortcut: `LOWER()`
is ASCII-only on SQLite, so a database case function answers «Шорты» to a
Postgres deployment and nothing to a SQLite one — the class of divergence
that makes a test suite agree with a stand that is wrong. `functions.fold`
is the wire normal form the schema documents, restated here rather than
imported because the two modules may not share a process; `ё` folds into `е`
(users type both), Cyrillic diacritics are kept (NFD would merge «мой» into
«мои»).

Minor, not patch (pre-1.0: minor = breaking): nothing existing changed, but a
new comm Function is a new surface, and `stapel-search>=0.7` names it.

### Settings

- `SUGGEST_INDEX_CACHE_TIMEOUT` (3600) — the ceiling on how long an
  *unchanged* tree keeps its folded name index. The entry is revision-keyed,
  so this is not how stale an answer can be.

## [0.8.4] — 2026-08-31

### Changed — the label snapshot crosses nothing this module emits, so only the cap moves

Patch (pre-1.0 semver: minor = breaking, patch = compatible). No model,
migration, view or code change: this release admits a sibling and does nothing
else. Every host that could install 0.8.3 installs this, and more can.

- **`stapel-attributes>=0.6,<0.7` → `>=0.6,<0.8`.** 0.7.0 snapshots the chosen
  options' `label` copy into `SelectDao.labels`, positionally aligned with
  `value`, so a card or a detail page spells a stored `select` without fetching
  the category config — it closes a live classified deployment that was
  printing storage slugs at people. Held at `<0.7`, this line is the wall
  rather than the guard for the second release running: pip answers
  `ResolutionImpossible` for anything installing this module beside the fix,
  and the fix is the whole point of the sibling's release.

- **The floor stays at `>=0.6`**, and 0.8.3 — one entry down — is precisely why
  that had to be checked rather than assumed. It moved the floor *with* the cap
  because the committed `docs/schema.json` NAMES `group`: on 0.5 the
  discriminator mapping this module emits advertised a type the installed
  registry did not have, which is the same lie as a missing one pointing the
  other way.

  Nothing of 0.7.0 is named here. `labels` is a field on a stored DAO, and this
  module emits no DAO at all — `docs/schema.json` carries zero `*Dao*`
  components, because what categories generates is the *config* side:
  `FeatureConfig`'s discriminator mapping and `FeatureDto`. The DAO is
  stapel-listings' half of the boundary, and the only `dto_to_dao` in this
  repository is a comment in `models.py` naming the FeatureDef fields that have
  to cross it so the title/badge projections are not built empty.

  Measured, not reasoned. Against 0.7.0 installed: `make contract-check` is
  clean — the triad regenerates byte-identical, so no `docs/{schema,flows,
  errors}.json` change appears in this release — and the suite is **330
  passed**, with `tests/test_contract.py` still pinning thirteen registered
  types. 0.7.0 adds a field to an existing type, not a fourteenth type.

  So `>=0.6` is what the committed schema requires and `<0.8` is what the fleet
  needs admitted. A host on stapel-attributes 0.6.x takes this release; under a
  floor of 0.7 it would have been stranded on 0.8.3 for nothing.

Only the generated version strings move: `docs/capabilities.json`,
`docs/llms.txt` and `README.md`, refreshed by `make contract`.

## [0.8.3] — 2026-08-31

### Changed — the composite `group` crosses this module, and the cap widens to `<0.7`

0.8.2 capped `stapel-attributes` at `<0.6` to state the range it was tested
against. This release does the work instead.

- **`stapel-attributes>=0.6,<0.7`.** The floor moves with the cap, not just the
  cap: the committed `docs/schema.json` NAMES `group` in both
  `FeatureConfig.discriminator.mapping` and `FeatureDto`'s, and on 0.5 that
  mapping would advertise a type the installed registry does not have — the
  same lie as a missing one, pointing the other way. A host on
  stapel-attributes 0.5.x stays on stapel-categories 0.8.2.
- **Contract triad regenerated**: `GroupConfig` / `GroupDto` and their two
  mapping entries; `tests/test_contract.py` pins thirteen registered types.
- **`translation_keys` walks a composite's children.** A group's `config.fields`
  are full feature definitions, and they are *not* catalog rows — `walk_features`
  never sees them — so without this branch a child's name, help text and option
  labels reached no `.po` file and a subform rendered raw keys to the user with
  nothing failing. Order is `fields` order; depth is 1 by the engine's own rule.
- **`tests/test_resolved_feature_contract.py` covers the composite's shape.**
  `config` is exempt from the property gate because both sides describe it as
  an opaque `{type, ...}` object — an exemption that is only safe while nothing
  of consequence hides inside it. A group puts FeatureDefs in there, so the new
  check asserts the canon declares them by `$ref` to `FeatureDef` itself: a
  child's `rules` dropped in transit would be the same silent revert to static
  `mandatory`, one level down, and an inlined narrower child shape would ride
  inside the exemption ungated.
- **`tests/test_feature_group_kind.py`** (new, 12 cases) pins the crossings
  rather than the semantics: `Feature.clean()` accepts a composite and reports
  the engine's three refusals on the `config` field; `feature_defs()` and the
  `categories.features` Function carry the nested `fields` verbatim (with comm
  schema validation ON); the payload still builds a FeatureDef the engine
  validates values against; both read serializers carry the children; and an
  export → load → export round-trip of a composite is byte-identical.

No model, migration or view change.

## [0.8.2] — 2026-08-31

### Fixed

- **`stapel-attributes>=0.5,<1.0` → `>=0.5,<0.6`.** Pre-1.0 house semver reads
  a minor as breaking, and this line was the exception that proved it:
  stapel-attributes 0.6.0 adds a thirteenth built-in type (`group`), which
  changes the discriminator mapping this module emits, so a published release
  of that package started failing `tests/test_contract.py` here
  (`assert 13 == 12`) with nothing in this repository having changed — 0.8.1's
  own publish run is where it surfaced.

  Lifting the cap to `<0.7` is work rather than a number: regenerate the
  contract triad against 0.6 and extend the `ResolvedFeature` gate for the
  composite kind. Until then this release states the range it is actually
  built and tested against, which is also the range stapel-shop (`<0.6`) and
  stapel-classified (`>=0.5.1,<0.6`) install.

  No code change: 0.8.2 is 0.8.1 with a cap that matches its own test matrix.

## [0.8.1] — 2026-08-31

### Fixed

- **`load_catalog` rebuilds the tree cache once per load, not once per row.**
  django-treenode maintains its denormalized columns (`tn_ancestors_pks`,
  `tn_level`, `tn_order`, …) from a `post_save` / `post_delete` receiver that
  rebuilds the **whole table**: one read of every row plus one `UPDATE` per row,
  for every single row written. A load of N rows therefore cost O(N²)
  statements against a heap no autovacuum can touch inside the load's own
  transaction, so the real curve was worse than quadratic. Measured on the
  imported catalog fixtures (postgres 16, one transaction, no deletes):

  | rows written | 0.8.0 | 0.8.1 |
  |---|---|---|
  | 32 features / 3 categories | 0.6 s | 0.4 s |
  | 64 features / 4 categories | 1.5 s | 0.7 s |
  | 134 features / 8 categories | 5.1 s | 1.4 s |
  | 240 features / 17 categories | 63.3 s | 3.8 s |
  | 430 features / 51 categories | did not finish (killed at 10 min) | 6.0 s |
  | 14 409 features / 3444 categories (52 488 links) | did not finish (killed at 15 min, still in the feature phase) | 185 s |

  The write phase now runs with those two receivers suspended and calls
  `Feature.update_tree()` / `Category.update_tree()` once at the end, inside the
  load's transaction — which is what treenode itself does inside its own bulk
  operations. `update_tree` is a pure function of the committed `tn_parent`
  edges, so no row ends in a different state; a failed load rolls the
  denormalized columns back with the rows, and the receivers are restored even
  when the load raises. A re-run of that last row with nothing changed is 23 s
  of pure diffing — the load is still idempotent to the record.

  Nothing the H-2 rule is about is suspended: `full_clean`, `save`, the revision
  bump, `category.changed` and `copy_parent_features` still run per row, because
  they are model and stapel receivers rather than treenode's.

## [0.8.0] — 2026-08-31

**Minor = breaking** (pre-1.0). `load_catalog` stops keying a re-import on the
slug. A fixture row that carries an external id is now matched against the
live row with the same `(external_source, external_id)` **first**; only a row
without one falls back to the slug.

### Why the slug could not stay the key

An imported category's slug is derived from the source catalogue's node path.
A client fleet re-syncs an imported marketplace tree as that source's schema
moves, and when the source renames a node its path — and so its slug —
changes while the node id does not. Keyed on the slug, that re-import reads as "one category
disappeared, an unrelated one appeared": `load_catalog` would (soft-)delete
the row holding the listings and create a duplicate beside it. Keyed on the
node id it is what it actually is — one row, updated in place, its slug moving
with it.

### Added

- `Category.external_source` (`CharField(32)`, blank) — which catalogue
  `external_id` belongs to. The re-import key is the **pair**, because
  `external_id` alone is the source's own numbering and two catalogues
  numbering from 1 would silently collapse onto each other's rows. Blank means
  "the deployment's only import source" — the value every existing row carries
  and the value a fixture omitting the key matches, so a single-source catalog
  never has to set it and matching degrades to plain `external_id` there.
- Index `cat_category_extid_idx` on `(external_source, external_id)` — the
  lookup the loader now does per record. Deliberately **not** unique: a
  catalog may legitimately hold two rows for one source id (a hand-split
  node), and the loader reports that ambiguity itself instead of letting the
  DB refuse an unrelated write.
- Migration `0004_category_external_source` — expand-only (one `AddField`,
  one `AddIndex`).
- `Report.renames` and `Item.renamed`, so the plan can count and mark renames
  apart from the adds and removes they would otherwise have been.

### Changed

- **`load_catalog` matching precedence.** `(external_source, external_id)`,
  then `slug`. A matched row is updated in place — name, parent, features,
  flags, and the slug, which is what the rename *is*.
- **The `--dry-run` plan diffs by identity too**, not just the writes. The DB
  view and the sidecar base are re-keyed onto the renames before the 3-way
  classification runs, so a renamed node classifies as one ordinary
  fast-forward under its new slug instead of a delete of the old key plus a
  create of the new one. A host's "what would a re-sync do" target reads
  the truth.
- **Renames print distinctly**: `» slug 'a' → 'b' (external_id 'X')` against
  `+` add / `-` remove / `~` update, plus an `of which renamed N` on the
  summary line.
- Three things the loader refuses per record, loudly, rather than guessing:
  two live rows claiming one source id; a rename whose target slug is held by
  a row this import does not move (identity wins the *match* — so the rename
  is what gives way, and the other row is never clobbered); and a cycle of
  renames, where no order frees both slugs. A rename **chain** (`a→b` while
  `b→c`) is not a refusal — the holder is sequenced first and both land in one
  run.
- A slug-matched row whose stored identity the fixture overwrites is still
  applied (the fixture is canon for its own slug — correcting a wrong id must
  work) but is now called out in the plan as a re-stamp.
- `external_source` rides the `Category` serializers and the admin changeform,
  and `external_id` joins the changelist search fields.

### Not changed, deliberately

- **Feature bindings keep keying on the feature slug.** The root
  `Feature.slug` is the source's own tag (`brand`, `screen_condition`), not a
  path-derived string — it does not move when a category is renamed, and a
  category's inline overrides are resolved per `(category, root slug)` off it.
  Nothing there is unstable, so nothing there changed.
- **The fixture files still address categories by slug** — `parent_slug`
  edges, sidecar keys, record order. Identity is how a row is *found*, not how
  it is *addressed*.
- **No sidecar version bump.** `external_source` is written only when set
  (like `is_test`), so every content hash a 0.7.0 export wrote still means
  what it meant and `.sync-state.json` stays at version 2.

## [0.7.0] — 2026-08-30

**Minor = breaking** (pre-1.0). Slice S3 of the attributes-v2 architecture:
`Feature` grows conditional rules and form metadata, `Category` grows
`external_id`, and every seam those cross now carries them.

What is breaking: the fixture record shape changed on both sides (a feature
record gained six keys, a category record gained one), so **every content
hash a 0.6.x export wrote is stale** — `.sync-state.json` version goes 1 → 2
and `load_catalog` refuses an older sidecar with "regenerate via
export_catalog" rather than reading the whole catalog as conflicted. The
`categories.features` payload gained six required properties. And the
stapel-attributes floor moves to `>=0.5,<1.0`, whose own breaking changes
(requiredness is `RuleState.required`, not `FeatureDef.mandatory`; a `ref_*`
config needs a registered resolver) reach anything mounting this module.

### The half of a feature definition that was not crossing

99.9 % of the fields in the imported catalogue dataset carry a description and
an example,
31 % carry a dependency (a conditional rule), and none of that had anywhere to
live here. stapel-attributes 0.5.0 gave `FeatureDef` the six fields for it;
this release stores them and — the actual work — walks every place a feature
definition crosses a boundary and makes it carry them.

That walk is the point. Nothing *fails* when a serializer field list, the
fixture writer or the editor's create path quietly omits `rules`: the answer
just comes back smaller, the category silently reverts to static `mandatory`,
and the form renders without its help text. So the new
`tests/test_resolved_feature_contract.py` gates
`schemas/functions/categories.features.json`'s `$defs.ResolvedFeature`
against **the canon** — `stapel-attributes/docs/feature-def.schema.json`,
§68's one-JSON-many-emitters — asserting it covers and requires every canon
property except `config`. It reads the canon from a sibling checkout when the
workspace has one and from the installed package otherwise (the schema is
package data), so it never degrades to a skip: the next `FeatureDef` field
upstream adds fails this module's suite until it is carried through.

### Added

- `Feature.rules` (`JSONField`, default `[]`) — conditional rules in
  stapel-attributes' closed grammar (require / show / hide / forbid_option /
  limit). A **sibling of `mandatory`, never part of `config`**: a rule is
  type-independent, while `config` is parsed by the per-type serializer.
- `Feature.description` / `example` / `default` / `hints` / `group` — the form
  metadata (help text, placeholder, initial value, notices, section). Each is
  a translation key or a literal, resolved like `name`, and none of it ever
  lands in a stored listing value.
- `Category.external_id` (`CharField(64)`, indexed, blank) — the identifier
  the category carries in the catalogue it was imported from. Opaque, not
  unique, and **not** a natural key: fixtures still address categories by slug.
- Migration `0003_feature_rules_form_metadata_category_external_id` —
  expand-only, seven `AddField`s.
- `Feature.clean()` parses `rules` through `parse_rules` and reports a
  deviation on the `rules` field (not on `config`), and checks that `hints` is
  exactly `[{"title": str, "content": str}, …]`.
- `validators.feature_warnings(category) -> list[str]` — the findings only the
  *whole* resolved feature set can answer: a rule condition, or a ref type's
  `optionsRef.parentFeature`, naming a slug the category does not define. It
  **never raises**, deliberately: the same feature is reused across categories
  with different field sets, where an unknown controlling slug legitimately
  reads as `empty`. Review material, not a gate. Exported on the surface.
- `tests/test_resolved_feature_contract.py` (5 tests) — the canon gate above.
- `tests/test_feature_rules_metadata.py` (41) and
  `tests/test_catalog_rules_metadata.py` (12) — one assertion per crossing:
  `feature_defs()` rebuilding a lossless `FeatureDef`, the five serializers,
  the editor's create/edit/inherit, the fixture pair (byte-stable, an
  idempotent all-skips second load, a sidecar hash that actually moves when a
  rule set does), `validate-dto` requiring a feature only once a rule shows
  it, the admin changelist/changeform, and `GET /features` serving a
  `ref_select` config verbatim.
- `tests/fake_vocabulary.py` — a local in-memory `VocabularyResolver`, since
  0.5.0 makes a `ref_*` config loud without one and stapel-attributes' own
  test fake is not shipped in its wheel.

### Changed

- **Every feature-carrying boundary now carries the six fields**:
  `Category.feature_defs()` and `get_feature_schema()`; `FeatureSerializer`,
  `FeatureCompactSerializer`, `FeatureBulkSerializer` /
  `FeatureCreateUpdateSerializer` (via `__all__`) and
  `FeatureEditorFeatureSerializer`; `feature_editor.py`'s create / edit /
  inherit; `catalog_fixtures.py`'s root record and inline-override entry (and
  therefore the content hash); `catalog_load.py`'s `_INLINE_KEYS`,
  normalizers, `_FEATURE_SCALARS`, upsert and override materialization;
  `admin.py` (a "Form" fieldset and a "Rules" fieldset, plus `group` on the
  changelist) and `forms.py`. `external_id` rides the Category serializers,
  fixtures, loader and admin.
- `translation_keys.py` collects `description`, `example`, `group` and each
  hint's `title`/`content` alongside `name`, under the same `translate` gate —
  so the `collect translation keys` endpoint lists what the form actually
  renders instead of leaving raw keys on screen.
- `$defs.ResolvedFeature` gains the six properties, **all required with a
  documented default**. Required, not optional: "the producer may omit it"
  means "requiredness may silently fall back to `mandatory`", so the response
  sends them blank/empty rather than absent.
- `catalog_fixtures.STATE_VERSION` 1 → 2 (see above).
- `catalog_load._materialize_override` distinguishes "this entry says nothing
  about that field" from a stored `None` with an `_UNSET` sentinel instead of
  `None`. Latent before, load-bearing now: `default = None` is a real value
  ("the form starts empty"), and conflating the two would leave a fixture edit
  unapplied and re-detected on every subsequent load.
- Floor `stapel-attributes>=0.5,<1.0`.
- `tests/test_contract.py` pins twelve registered type slugs (was ten):
  0.5.0 added `ref_select` and `ref_hierarchical_select`.

### Unchanged, deliberately

- `GET /categories/{id}/features` still serves `config` **verbatim** — a
  `ref_select` arrives as its `optionsRef` pointer and nothing else. The
  vocabularies behind it run to ~15 000 terms per level; inlining them at this
  endpoint is not a size problem to optimize later, it is the wrong contract.
  Pinned by a test.
- `Feature.mandatory` stays a static flag. Conditional requirement is a rule;
  the two are siblings, not replacements.

## [0.6.2] — 2026-08-30

Patch (pre-1.0 semver: minor = breaking, patch = compatible). Tests only —
no behaviour, route, schema or settings change.

### The public read was true but unowned

The category tree and the feature schema are the navigation of a storefront:
every page a search engine indexes renders them, and none of that traffic
carries a session. That anonymous reads work rests on one line on each
viewset — `permission_classes = [ReadOnlyOrStaff]` — and nothing asserted it.

Every existing HTTP test in this repo authenticates a superuser first
(`force_authenticate`, `test_category_commands.py`), so the whole suite is
blind to exactly this: swap `ReadOnlyOrStaff` for `IsStaffUser` and it stays
green while every catalogue page on the internet answers 401. A green gate
that cannot see the thing it is supposed to protect is worse than none,
because it is trusted.

### Added
- `tests/test_public_read.py`. A client with **no credentials at all** gets
  200 on the category list, retrieve and `children/`, and on the feature list,
  retrieve and a category's `features/`. None of those responses carries a
  `Set-Cookie` — the read must stay cacheable at the edge and must not start
  a session per crawler. Anonymous `POST`/`PATCH` of a category is refused
  and writes nothing, and is **401** rather than 403 wherever the deployment's
  authenticator offers a challenge, which is what a fleet on
  `JWTCookieAuthentication` returns. Both permission classes are asserted by
  name, so a regression fails a test that says what broke instead of eight
  that say `401 != 200`.

## [0.6.1] — 2026-08-22

Patch (pre-1.0 semver: minor = breaking, patch = compatible). Bug fix
inherited from upstream — no route/component/error-key change of its own.

Filed by @stapel/categories-react (the storefront spec §13.7
note 5): `docs/schema.json`'s `FeatureConfig`/`FeatureDto` discriminator
mapping had a single bogus `"null"` entry instead of the ten feature-type
slug entries, because stapel-attributes' `PolymorphicProxySerializer` was
built from a bare list of serializer classes and drf-spectacular's
resource-type inference collapsed every sub-serializer to `None` (fixed
upstream in stapel-attributes 0.4.7 — see its CHANGELOG for the full
root-cause writeup).

### Fixed
- Floor bumped to `stapel-attributes>=0.4.7,<0.5` and `docs/schema.json`
  regenerated (`make contract`): `FeatureConfig`/`FeatureDto`'s
  `discriminator.mapping` now carries all ten slug-keyed entries
  (`int`, `float`, `string`, `bool`, `hex_color`, `select`, `date`,
  `header`, `hierarchical_select`, `convertible_unit`), fixing the
  openapi-typescript codegen that previously stripped `type` from call
  sites and re-added a synthetic wrong one.
- Added `tests/test_contract.py::test_feature_config_discriminator_is_slug_keyed`
  asserting the committed `docs/schema.json` mapping is slug-keyed, matches
  the registered type slugs 1:1, and never contains `"null"`.

## [0.6.0] — 2026-08-22

**This module now emits its own contract triad.** `docs/schema.json`,
`docs/flows.json` and `docs/errors.json` did not exist before this release —
the Makefile said so out loud — which blocked the react codegen pipeline
(`gen:api`/`gen:errors`/`gen:manifest`) for any `-react` pair generated
against this module (the storefront spec §1.8, §3.10, A1).

### Added

- `_codegen.py` + `_codegen_settings.py` + `codegen_urls.py`: a
  single-module `{categories + core}` Django harness that emits
  `docs/{schema,flows,errors}.json` at the canonical `/categories/api/v1`
  prefix, the same mechanism stapel-search/-chat/-forms already use.
  `make contract` / `make contract-check` now cover the triad in addition
  to the existing `surface`/`docs/llms.txt`/README.md gates.
- `docs/schema.json` (23 paths), `docs/flows.json` (`[]` — no `@flow` is
  declared yet, same state as every other contract-complete module today),
  `docs/errors.json` (62 keys: 8 owned by this module, the rest inherited
  from stapel-core/stapel-attributes).
- `tests/test_contract.py`: every mounted route is described in
  `docs/schema.json`; every `STAPEL_CATEGORIES_ERRORS` code and every
  stapel-attributes validation code the feature-editor/validate-dto paths
  can raise is declared with the correct `owner`.
- `docs/llms.txt`'s token budget raised 4000 → 5000 (`--budget 5000`, same
  exception stapel-forms/-recordings already take) — the errors + HTTP
  operations sections pushed it over the default ceiling.

### Changed

- `FeatureBulkSerializer.config` and the `convert-type` action's request
  `config` field now resolve through the same `FeatureConfig` discriminated
  `oneOf` as `FeatureCreateUpdateSerializer.config`, instead of a bare
  `JSONField`/`DictField`. `ValidateDtoRequestSerializer.features` is now a
  `{slug: FeatureDto}` object (the new `FeaturesDtoField`, mirroring
  stapel-listings' `ListingFeaturesInputField`) instead of an untyped
  `JSONField`. Response bytes are unchanged; only the declared OpenAPI type
  is — three fields that used to fall back to "any" now describe the same
  ten-way polymorphic shape the rest of the config/DTO surface already did.
- `docs/readme.md`: the quick-start mount snippet corrected to
  `path("categories/api/", include("stapel_categories.urls"))` — this
  module's own `urls.py` bakes in only `v1/`, the host contributes `api/`,
  exactly the recipe stapel-example-monolith already uses. Plus a
  documented, deliberate gap: `FeatureValidationResult.id`/`.ref_value`
  stay untyped because they are stapel-attributes' scalar-union fields, not
  this module's to type (see the README "Contract" section).

## [0.5.6] — 2026-08-21

### Added — `categories.path`, the ancestry provider the fleet declared before anything answered it

stapel-search 0.1.0 named `CATEGORY_PATH_FUNCTION = "categories.path"` as the
canonical way to resolve a category's ancestors, raised `search.W006` because
nothing in the fleet answered it, and degraded `category_path` to a single
segment — meaning an exact category filter worked and a filter on a *parent*
category found none of its descendants. This module owns the tree, so it is
the only place that can answer without re-deriving the hierarchy from outside.

`{"category_ids": [id, ...]}` -> `{"<id>": ["<root_id>", ..., "<id>"]}`.
Root first, the category itself last; a root answers a one-element path; an id
with no row is **absent** from the mapping rather than mapped to an empty path,
so "no such category" stays distinguishable from "a root". Segments are
category **ids**, not slugs — the declared consumer feeds the last segment of a
requested path straight back into `categories.features`, whose payload is typed
as an integer id, and slugs would fail that call silently and take the whole
facet plan down with them.

One query for a whole batch: django-treenode already denormalizes ancestry into
`tn_ancestors_pks`, so this is a read of a column the tree maintains, not a
second hierarchy of ours. Held by a test that counts the queries.

### Fixed — `emit-check` was red on two signal receivers

`EMIT005` flagged `emit_category_changed_on_save` /
`emit_category_changed_on_feature_save` as "declared but never called": both are
`@receiver(post_save)` handlers, dispatched by Django rather than from a call
site in this package. Annotated with the lint's own escape hatch, so the gate
is green *and* says why — it was failing CI for every commit, which is the same
as having no gate.

## [0.5.5] — 2026-08-15

### Changed — `stapel-core` floor raised to 0.26.0

The floor sat at `>=0.10`, far below the core this app is developed and
tested against. 0.26.0 is the core whose error registry runs the
registry-catalog pairing gate that the deterministic `stapel_attributes`
registration in this release exists for; the floor now names it instead of
leaving a consumer free to resolve a decade-old core.

## [0.5.4] — 2026-08-03

### Fixed

- Пол зависимости на `stapel-attributes` поднят с `>=0.3,<0.4` до `>=0.4,<0.5`.
  Старая ветка attributes кэпала `stapel-core<0.12`, из-за чего categories
  нельзя было установить рядом с profiles/notifications/workspaces/cdn,
  требующими `core>=0.16` — `ResolutionImpossible`. Собственный потолок на
  core расширили до `<1.0` раньше (3e3200f), а пин на attributes расширить
  забыли: библиотека была невыпускаема в сборку с флотом, и это не ловилось
  ничем, потому что в изоляции её тесты зелёные (213 passed на attributes 0.4.4).

All notable changes to stapel-categories are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Pre-1.0 semver: **minor = breaking**, patch = compatible.

## [0.5.3] - 2026-08-02

Packaging / contract only. Patch.

### Added
- `docs/llms.txt` — the fifth contract artifact — is now emitted, drift-gated
  by `make contract`/`contract-check`, and badged in the README.
  `docs/capabilities.json` remains hand-written (stapel-catalog sweep); these
  targets manage only `docs/llms.txt` and never touch `capabilities.json`.
  No `surface` entries exist yet, so the generated llms.txt's Usage surface
  section is empty (pre-existing gap, not introduced here).
- Badge canon, Python 3.14 classifier, migration-lint enabled in CI.

### Fixed
- `docs/capabilities.json`'s hand-maintained `version` field had drifted to
  `0.5.1` (missed the 0.5.2 bump); corrected to match `pyproject.toml`.
  Content unchanged.
- `docs/llms.txt`/`docs/capabilities.json`/`docs/flows.json`/`docs/errors.json`/
  `CONFIG.MD` are now listed in `package-data` so they ship in the wheel.
- CI now tests Python 3.14 (the version actually deployed).

## [0.5.1] - 2026-07-17

Fleet follow-up to stapel-core 0.12.0 (legacy shim sweep). No source
changes needed. Full suite green against core 0.12.0.

### Changed
- `stapel-core` dependency ceiling `<0.12` → `<0.13`.

## [0.5.0] - 2026-07-17

Legacy sweep: pre-0.2.0 backward-compat shims removed (breaking → minor per
pre-1.0 semver).

### Removed
- **`base_revision` is no longer optional** on the feature-editor apply path.
  `apply_feature_editor_changes(category, items, base_revision)` now requires
  the token, and `POST …/feature-editor/apply/` rejects a payload without
  `base_revision` with `400` (`FeatureEditorApplySerializer` field is
  `IntegerField()` — no `required=False`, no `allow_null`). The "omit to opt
  out of the optimistic check" compat behavior for pre-0.2.0 clients is gone:
  every apply is now revision-checked (stale → `409`), closing the lost-update
  loophole for clients that silently never sent the token. Clients must echo
  the `revision` from the feature-editor state response.

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
