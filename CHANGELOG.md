# Changelog

## [0.20.3] — 2026-09-04

### Fixed

- **A fixture no longer erases what it does not state.** After a full
  `load_catalog --on-conflict fixture-wins` reload on a live stand, three of
  the four chip rows answered `children_axis_label: ""` — every caption
  `derive_children_as --apply` had written the same afternoon was gone, and
  the fourth kept its own only because that one record happened to spell it
  out. A storefront then drew every partition uncaptioned, and the next
  derivation run had all of its work to do again.

  Root cause: 0.20.0 put `children_axis_label` in `_CATEGORY_SCALARS`, and
  `_apply_category_upsert` read it as `record.get("children_axis_label") or ""`
  — but the EXPORT writes that key only when it is set, so an absent key is a
  shape canon itself produces on every category that has no caption. Absence
  was applied as an instruction to blank the column. The same line stood over
  `children_as`, and the same `.get(key, "")` reading over `comment`,
  `external_id`, `external_source` and `translatable`.

  The rule now, for every optional category scalar
  (`catalog_load._OPTIONAL_CATEGORY_SCALARS`): an **absent** key keeps the
  live value; an **explicit** key is applied, `""` and `auto` included — that
  is how a fixture clears a column, and it converges in one pass rather than
  reporting a residual forever. The curation keys (`catalog_icon`,
  `carousel_icon`, `carousel_enabled`, `active`) already had this cure in a
  stronger form — write-once, hash-stripped, since 0.15.0 — and keep it;
  `slug`, `parent_slug`, `name` and `features` are deliberately outside the
  rule, being identity, structure and content the export always states (and
  for `parent_slug`, an absent key and an explicit `null` are the same "this
  is a root").

  The plan half matters as much as the write: an unsaid key is HASHED as the
  value it keeps (`_optional_projection`, the same trick the override-name
  rule uses), so such a record classifies as unchanged. Without that, a
  catalogue whose captions live only in the DB would read `updated` for every
  derived row on every pass — the churn 0.20.2 was released to end — and a
  db-side value the fixture never mentions would report as drift the operator
  is told to export.

  Because the erasure was silent, the load now SAYS what it kept: `kept N live
  value(s) the fixture does not state (children_axis_label 3)`, per key, in
  both the dry run and the real load (`Report.kept_unsaid`). A count of
  defaults kept is not reported — keeping a default is keeping nothing.

  `derive_children_as --apply` is therefore idempotent across a reload again,
  which is pinned end to end: derive → apply → load a fixture that does not
  mention the column → the caption survives → derive again prints "Nothing to
  write." No migration, no sidecar bump: `STATE_VERSION` stays 5 and every
  hash on disk keeps its meaning.

## [0.20.2] — 2026-09-04

### Fixed

- **The catalogue load converges.** On a live catalogue (3444 categories,
  3192 root features, `load_catalog --on-conflict fixture-wins --deletions
  ignore`) a full load planned 431 category updates, the next pass planned
  303, and every pass after that planned the same 303 — zero conflicts, zero
  creates, converging never. The apply itself wrote nothing (the dirty guard
  in `_apply_category_upsert` held), so nothing was corrupted; what was broken
  was the plan an operator reads and the "a re-run is a no-op" property the
  whole 3-way diff rests on.

  Root cause: the sidecar recorded ONE hash per key — the DB hash the apply
  produced. For a record whose applied row cannot hash equal to its fixture
  record, that is a number the fixture side can never reach, so the next pass
  read it as "the fixture moved" and re-planned the write. Forever.

  The sidecar now records the PAIR the sync established (`{"fixture": …,
  "db": …}`) and `_classify` asks **which side moved since that sync**, not
  which two hashes look alike. A record whose fixture side did not change and
  whose DB row nobody touched is `skipped` — even where the two hashes differ.
  The 3-way diff is unblunted: a real DB-side edit moves the DB half and is
  still `db_only` (or `conflict` when the fixture moved too), which is pinned
  by a test. A record that lands not-equal is now REPORTED, once, as
  `residual` — "applied, but an export would write something else" — rather
  than absorbed silently or re-planned every pass; a base that quietly
  swallows a difference nobody was told about is a gate that proves nothing.

  `STATE_VERSION` 4 → 5. A v4 sidecar is still READ (a bare string is both
  halves of the pair, so every key classifies exactly as it did under 0.20.1)
  and upgrades itself key by key on the next load — no regeneration step, no
  re-export. The bump is for the other direction: a pre-0.20.2 loader must
  refuse a sidecar holding pairs loudly instead of reading every pair as a
  two-sided change.

- **An override's name follows its root when the fixture does not restate it.**
  112 of those 303 rows carried one of the 12 feature slugs whose two fixture
  directories share a namespace. The export writes an override's `name` **only
  when it differs from its root's**, so an absent one means "the root's" — but
  the loader read absent as "leave whatever is there". Once canon renamed the
  root, the per-category clone kept the label the root had dropped, the export
  wrote that stale label out, and the record's DB hash could never equal its
  fixture hash again. Sellers saw the retired label too, which is the same
  failure this module already fixed in the other direction (0.13.0, the
  cookware leaf asking a clothing question).

  The loader now writes the root's name for an entry that says nothing, and
  the fixture side is hashed by the export's own rule (an entry restating its
  root's own label is hashed as the absence it will be exported as). A clone
  shared with other categories is copied-on-write for the relabel, as for any
  other inline difference. A root rename lands one pass ahead of the
  categories that inherit its label — the category plan is classified against
  the DB as it stands before the same run's feature upserts — so such a wave
  needs two passes, not endless ones.

  Measured against a synthetic catalogue shaped like that plan (404
  categories, 303 of them in the two classes): 0.20.1 plans 203, then 303,
  303, 303…; 0.20.2 plans 12, then 112 (the stale labels, repaired), then 0
  and 0.

## [0.20.1] — 2026-09-04

### Fixed

- **A `chips` parent answers the schema its own page needs.** A partition
  parent (`Автомобили` over `Новые`/`С пробегом`, a real-estate node over
  `Куплю`/`Продам`/`Сдам`/`Сниму`) renders the feed and the chip row for the
  whole partition since 0.19.0 — but asked for its features it still answered
  with its OWN links, which on such a node are none. The cars page offered no
  filters, the "popular values" block had no group to draw and the composer
  opened no fields until a chip was picked, which is the click the chip row
  exists to remove.

  It now answers the EFFECTIVE schema: the INTERSECTION of its children's,
  keyed by `slug` — the same key `get_all_features` already means by "the same
  feature". A feature only SOME children carry is deliberately not in it; it
  appears when its chip is chosen, because a control that half the feed cannot
  answer is a filter that hides listings. Where the children disagree about a
  feature the bounds WIDEN rather than one child's being picked — the lowest
  `min`/`minLength`/`minSelected`/`minDate`/`minDepth`, the highest `max`/…,
  the union of `options`, a bound unbounded in any child unbounded here,
  permissive booleans (`allowCustom`, `allowFuture`, `allowPast`) true if any
  child allows it and restrictive ones (`lockInput`, `lockUserInput`) false if
  any child lets go — and the feature carries `divergent: true`. So a client
  that renders it refuses nothing a child would accept, and a client that
  would rather not show a control meaning something different per chip can
  hide it until a chip is picked. `mandatory` follows the same rule: required
  here only where every child requires it.

  Comparison and merge run on the DEFAULTS-RESOLVED config, not the stored
  one: two children spelling one shape differently (one omitting a key, one
  writing its default) are not a divergence, and the merged config carries the
  keys a client already gets from a leaf.

  The order is the one the module already applies — the reference child's
  `get_all_features()` (own order first, then ancestors'), restricted to the
  intersection. There is no second ordering to disagree with the first: the
  composer that puts required-bearing blocks first, and required first inside
  a block, reads the same list for the parent as for the leaf under it.

  Three cases deliberately do NOT move, and are pinned: a leaf, a `tiles`
  parent, and a `chips` parent that declares features of its OWN. The last is
  **own only** — never own plus the intersection, which would be a third
  schema nobody authored; a parent carrying its own links has already had the
  decision made by hand, and an authored answer wins here as it does for
  `children_as`. A `chips` parent whose children are all retired has nothing
  to intersect and falls back to its own.

  Both readers answer it, since the composer reads one and the search plan the
  other. `GET /categories/api/v1/categories/<id>/features/` keeps its bare
  ARRAY body — every client of it reads one — so the one piece of meta rides
  as the `X-Effective-From: children` response header (`own` otherwise),
  declared in the contract; `divergent` rides on the feature it describes and
  is ABSENT rather than `false` where the children agree, so a leaf and a
  `tiles` parent answer byte-for-byte what they answered before. The
  `categories.features` Function gained `effective_from` beside `revision`,
  and `divergent` on the same features.

  Cache invalidation follows the existing revision fingerprint, widened to the
  rows the read actually touched: on the children path the `revision` the
  Function reports is the max over the parent and the children it intersected.
  A child's edit bumps the CHILD's revision and not the parent's, so a
  consumer caching by the parent's number alone would have held a stale
  intersection until its TTL — which is the whole class of bug the (revision,
  features) snapshot retry already exists to prevent, one edge out.

## [0.20.0] — 2026-09-04

### Added

- **`categories.by_slug` — the tree answers to slugs too.** stapel-search
  0.14.3 lets a `category=` segment be a slug, so a page whose address reads
  `/c/avtomobili` can ask for its own feed; the resolution needs a node, and
  nothing in the fleet could turn a slug into one. The Function is
  `categories.path` keyed by the other unique column: `{"slugs": ["transport",
  "avtomobili"]}` -> `{"transport": ["141"], "avtomobili": ["141", "151"]}`,
  ids as strings on both sides so a JSON round trip cannot change a value
  type, `maxItems: 1000` in the schema like `categories.path`.

  It is answered here for the reason `categories.path` is: this module owns
  the tree, and any other answer re-derives the hierarchy from the outside.
  The consumer declared the name (`CATEGORY_SLUG_FUNCTION`) before a provider
  existed, and until now every slug segment of a search query degraded.

  The conventions are the ones the consumer's contract fixes, and each of
  them is load-bearing: a slug with no row is simply ABSENT (the
  `projections.read()` convention) — absence is what stapel-search turns into
  its `error.400.search_unknown_category`, so it must not be an error, an
  empty list or a null; there are no errors of this Function's own, because a
  provider that cannot answer degrades at the caller and an outage may never
  be printed as a bad request; an INACTIVE row still answers, as
  `categories.names` does, since a listing can sit in a category retired
  after publication and its feed still has an address; a soft-deleted row
  does not answer at all. A RENAME stays invisible to the consumer's cache —
  `category.changed` carries an id and no slug — so its slug→path entries
  expire on `CATEGORY_CACHE_TIMEOUT` rather than being dropped; closing that
  would mean putting a slug in the event, and this release does not.

  Proven across the seam, not asserted: with stapel-search 0.14.3 and this
  build co-installed in one process, `category=avtomobili` over a two-node
  tree resolves to the id path `1/2` and echoes `category_resolved:
  {"path": "1/2", "slugs": ["transport", "avtomobili"]}` with an empty
  `degraded[]`; with `CATEGORY_SLUG_FUNCTION` pointed at a name nobody
  registers, the same query leaves the segment standing and degrades — which
  is what makes the first result an answer FROM this provider rather than a
  coincidence.

- **`children_axis_label` — a chip row says what it splits on.** «Все | С
  пробегом | Новые» is a set of values, and only the parent can say they are
  values OF something («Тип автомобиля»). The name of that axis is a fact
  about the set, not about any one child, so it is an optional column on the
  PARENT: a translation key like `name` (this module stores keys and the
  reader resolves them, so one catalogue captions its rows in every language
  the fleet ships), empty by default — a storefront then draws the row
  uncaptioned, exactly as every catalogue does today.

  It rides on every public read next to `children_as` (the frozen
  `PUBLIC_CATEGORY_KEYS` set grew by one, deliberately), on `GET /tree/`, on
  the staff serializer as a writable key, in the admin's Presentation
  fieldset, and in the catalogue fixture — written only when named, so no
  content hash on disk moves and `STATE_VERSION` stays 4, and applied by
  `load_catalog` on update: an axis caption is the same wherever the
  catalogue is loaded.

  ONE column, where `children_as` needed two. `derive_children_as --apply`
  now also names the axis of the parents it makes `chips`, from the
  vocabulary group it had already matched — deal type, condition, for whom —
  and what it can emit is a CLOSED set of translation keys, never a rendered
  word (a hard-coded Russian caption would make one market's alphabet the
  catalogue's). That closed set is what lets the command recognise its own
  previous answer and improve it on a re-run, so this field needs no
  derivation cache to stay re-runnable. Authored text always wins, in the
  read and again in the SQL guard of the write; a parent pinned to `chips` by
  hand still gets its axis named, and a `tiles` parent never does — there is
  no chip row to caption.

## [0.19.1] — 2026-09-04

### Fixed

- **A partition is a chip row even when some of its members have children.**
  On the first live derivation `Квартиры` and `Дома, дачи, коттеджи` came out
  `tiles` although their children are exactly `Продам`/`Сдам`/`Куплю`/`Сниму`:
  two of the four are split further, the `structure` signal ("a branch is
  never a chip") ran before the names, and it took the decision. That reading
  of structure was wrong about what a chip row holds. `Продам` having
  `Вторичка`/`Новостройка` under it does not stop the four transactions from
  being one partition of one template — it makes `Продам` a parent as well, and
  its own children are decided by these same rules. Nested chip rows are a
  legitimate shape, and the alternative on this catalogue was the exact cost
  the feature exists to remove: a buyer picking a page before seeing an item,
  on the two biggest branches of the tree.

  So the child NAMES are read first: a set that falls in one partition group
  is `chips` whatever the children's own shape. `structure` survives as a
  VETO where the names say nothing — schema overlap alone must not turn a
  shelf of branches into a chip row, since the overlap between two shelves
  says nothing about what is under them. Where the names overrode the veto the
  report's SIGNAL column says `vocabulary>structure`, because an operator
  reading a `chips` line on a node with grandchildren needs to see which
  evidence carried it.

### Added

- **`children_as` travels in the catalogue fixture.** `export_catalog` writes
  the AUTHORED column on a category record and `load_catalog` applies it, so a
  presentation decision survives DB → fixture → DB — the fleet bakes fixtures
  into images, and a value that does not round-trip is a decision the next
  container start forgets. It is catalogue content, not stand curation like
  the carousel keys: a partition of one template is a partition wherever the
  catalogue is loaded, so the fixture owns it on updates too, and an operator
  who authored something else has changed the DB side, which the 3-way diff
  reports as a conflict rather than silently losing.

  Written only when it is not `auto`, following the `external_source`
  precedent: `auto` is what every row says by default and the only thing any
  fixture written before this could say, so no content hash on disk moves and
  no sidecar regeneration is forced (`STATE_VERSION` stays 4).
  `children_as_derived` deliberately does NOT travel — it is a cache
  `derive_children_as` rebuilds from the loaded tree, and shipping it would
  freeze one run's guess into canon.

## [0.19.0] — 2026-09-04

### Added

- **A category says how its children should be drawn.** A storefront walking
  this tree had one shape for every level: a grid of tiles, one tile per
  child, whatever the children were. That is right where the children are real
  subcategories and wrong where they are a *partition* of one attribute
  template — `Куплю`/`Продам`/`Сдам`/`Сниму` under a real-estate node,
  `Новые`/`С пробегом` under cars, `Для мальчиков`/`Для девочек` under
  children's clothing. There the children ask the same questions of a listing
  and differ by one value the child's own name spells, so a tile grid makes a
  buyer pick a page before they can see a single item, and makes the seller's
  composer ask for a category twice.

  `children_as` is that answer, on every public read: `tiles`, `chips`, or
  `null` on a childless node. Children of a `chips` parent are untouched —
  same ids, same paths, same URLs, still the placement target of a listing.
  Only the presentation changes.

  Two stored columns, not one. `children_as` is the AUTHORED intent (`auto` by
  default: "nobody has decided"); `children_as_derived` is the derivation's
  cache. Collapsed into one column a derived value is indistinguishable from
  an authored one, so the next run would refuse to touch its own output and
  the derivation would be a one-shot instead of the re-runnable step a
  catalogue import needs. `auto` never crosses the API boundary: readers get
  `Category.resolved_children_as` — authored wins, else the cache, else
  `tiles` (the conservative half of the pair: tiles cost a click, a wrong
  `chips` hides a branch behind a filter nobody looks at). It reads three
  columns already on the row, so a page of N categories costs exactly the
  queries it cost before.

- **`derive_children_as`** answers the `auto` rows and shows its work: one
  line per parent with the node path, the decision, the SIGNAL that carried it
  and the number behind it. The signals are reported apart because they are
  wrong about different things — `structure` (a child with children of its own
  is a branch, and a branch is never a chip), `schema` (pairwise Jaccard ≥ 0.5
  over the children's own feature key sets), `empty-schema` (nothing anywhere
  at this node models a schema, so the children are not diverging in one) and
  `vocabulary` (the child NAMES fall in one partition group). The name signal
  is matched against the whole child SET and never a single name: one child
  called «Новые» beside twenty real subcategories is a subcategory that
  happens to be called that.

  Dry run by default. `--apply` writes only `children_as_derived`, only where
  `children_as` is still `auto`, with the guard repeated in the UPDATE so a
  value authored mid-run still wins — an authored `tiles`/`chips` is never
  overwritten, on any run. The writes are targeted UPDATEs rather than
  `save()`: a catalogue-wide re-derivation must not put a `category.changed`
  fanout through the fleet to record a presentation hint.

  The partition vocabulary is data in the command, not in the model — it is a
  fact about the catalogues a deployment imports, and in the model every
  deployment would inherit one market's words. Nothing in the derivation reads
  `external_id`/`external_source`: a rule keyed on an importer's node ids
  would silently do nothing for the next supplier. Where stapel-attributes is
  not importable the run says so in its first line and stands on the
  vocabulary signal alone.

- **`GET /categories/api/v1/tree/?depth=N`** (1..4, default 3) — the visible
  catalogue nested, in one call and one query, carrying `id`, `slug`, `name`,
  `path`, `catalog_icon`, `children_as` and `children`. A desktop mega-menu
  assembled from `roots` plus one `children` call per node is a request per
  branch on the storefront's coldest page; assembled from the flat list it is
  the whole table over the wire.

  `path` is the ancestor ids root→self, `/`-joined — the exact string a search
  query's `category` parameter takes, so a menu entry navigates without a
  second call to work out what it points at. Same `visible_categories()` rule
  the other three tree reads answer to, same order at every level, nested in
  Python off django-treenode's denormalised ancestry rather than a queryset
  per level, and cached on the tree's own revision fingerprint so a catalogue
  edit retires the entry immediately. An out-of-range `depth` is clamped, not
  refused: every answer this endpoint could give is still a correct prefix of
  the tree that was asked for. Provenance stays off it, like every other
  anonymous read — asserted on the tree, the list and the detail together.

### Fixed

- **The public reads no longer cost one query per category.** `features` is a
  plain PK list on the public projection and nothing prefetched it, so
  `roots`, `children`, `carousel`, the list and the detail each spent one
  extra query per row served — 3441 of them on a full imported catalogue's
  cold list. Prefetched on the viewset's queryset attribute and on the three
  actions that build their own, so a bigger page is now the same number of
  queries; `tests/test_children_as.py` measures a 2-root and a 12-root page
  against each other rather than pinning a constant that would drift.

## [0.18.0] — 2026-09-03

### Fixed

- **`--on-conflict fixture-wins` reverts a db-only EDIT, not only a db-only
  delete.** The two halves of db-only drift were asymmetric: a fixture-owned
  row DELETED in the DB was resurrected from canon under this policy, while a
  fixture-owned row EDITED in the DB was kept and warned about. So the flag
  meant "the fixture wins, unless the DB got there first" — and, worse, it did
  not converge: `db_only` is "base == fixture, db differs", so a record the
  fixture never changes again is classified `db_only` on every future run and
  re-running the load can never repair it. `abort` (the default) and `db-wins`
  are unchanged.

  What that cost on a live stand: two fixture directories legitimately shared
  one feature-slug namespace (a 20-leaf reviewed subset and the full 2901-leaf
  catalogue, whose importer picks a root feature's type by majority across the
  leaves it emits). The narrow one was loaded last and left **63 root features
  retyped** — `fuel_type` `select` → `ref_select`, `weight` `int` → `select`,
  `load_capacity`, `power`, `drive_type`, `parking` and 57 more. A root's type
  is what `Feature.clean` checks every per-category override against, so
  **15 category records failed with `Child config.type must match parent
  config.type` on every pass, and a second pass failed on the same 15** — the
  wider fixture had not changed, its roots were `db_only`, and `fixture-wins`
  declined to put them back.

- **A dry run reports the writes the apply would refuse.** `--dry-run`
  classified every record and printed a clean plan for the load above, which
  then errored on 15 of them: the plan never asked whether the rows it
  intended to write were writable. It now walks the overrides of every planned
  category upsert and reports an ERROR wherever an override's `config.type`
  contradicts the type its root feature will hold **after** this plan is
  applied (fixture type for a root the plan upserts, live DB type otherwise).
  The message names the feature, both types and the reason, and the run exits
  non-zero — the plan and the apply now agree about whether a load can succeed.

## [0.17.0] — 2026-09-03

### Fixed

- **A per-category override keeps its own display name.** A slug-bearing
  override row has a real `name` column, and both halves of the fixture round
  trip dropped it: the export wrote no `name` ("a slug-bearing override keeps
  the root's identity fields") and the load reset one to `root.name` on create.
  So DB → fixture → DB was lossy for the one identity field a category
  legitimately restates. An operator renaming an override in the admin lost the
  rename on the next export, and an importer had no way to say what its source
  said.

  Measured on one live catalogue import: **566 of the 760 leaves that offer
  `brand` render «Бренд одежды»** — the root's label, from the 172-leaf fashion
  majority variant — while their own source says «Бренд», «Производитель» or
  «Марка». Corpus-wide it is 3512 of 54208 (leaf, feature) pairs across 206
  slugs; `video_file_url` shows «Видеофайлы» on 1852 leaves that say «URL
  видеофайла». A cookware leaf asking a clothing question is not a cosmetic
  slip.

  `name` now travels on a slug-bearing override, **only when it differs from
  the root's** — the `external_source` precedent. A catalogue with no renamed
  override exports byte-identically to before, every content hash already on
  disk stays valid, and `STATE_VERSION` is unchanged: no sidecar regeneration
  on upgrade. Absent still means "inherit the root's name".

  `name` also joined `_INLINE_KEYS`, so a per-category RENAME is an override on
  its own: an entry may carry the root's config verbatim and differ only in the
  label it puts on the field. Without that it read as a bare reference and the
  rename was dropped on the way in — the mirror image of the export dropping it
  on the way out.

  Round-trip idempotence is pinned: a load of the fixture the live DB just
  exported reports zero updates.

- **A malformed category id answers `LookupError`, not a 500.** `Category.pk`
  is an `AutoField`, so `objects.get(pk="32/149/163")` — a search PATH where an
  id belongs, which is what three drafts on one live stand carried — raises
  `ValueError`, not `DoesNotExist`. It walked straight past
  `features_function`'s own `except` and surfaced as an unhandled fault, while
  every caller in the fleet is written against the `LookupError` this module's
  docstrings promise: stapel-listings' re-projection counts it as
  `category_unresolved`, its publish path turns it into a 400.

  The payload schemas do type these ids as integers, but that only holds while
  `VALIDATE_SCHEMAS` is on, and a contract conditional on a runtime flag is not
  a contract.

  `categories.children` had the same hazard on `parent_id` (a bare
  `filter(pk=…).exists()`) and the same fix. `categories.path` and
  `categories.names` already carried a private form of the guard — an
  `.isdigit()` filter on the incoming list — which is exactly why two of the
  five providers had none: three treatments of one hazard. There is one now,
  `_resolve_category` / `_category_exists`, and `None` still means "the roots"
  rather than a malformed id.

## [0.16.1] — 2026-09-03

Patch. Cap only: `stapel-attributes>=0.8.3,<0.10`.

stapel-attributes 0.9.0 changes one rule semantic — a VALUE predicate (`in` /
`not_in`) no longer matches a controller that reads EMPTY, so a
`require when X not_in […]` rule stops firing before anyone has answered `X`.
Two UX walkers had hit that wall on an imported catalogue: a field starred and
refusing "Next" while its own help line said it was needed only *if* another
field said so, with that field untouched.

This module STORES and VALIDATES rules (`Feature.rules` -> `parse_rules`); it
does not evaluate them for a form, and the grammar `parse_rules` accepts is
unchanged. The whole suite is green against 0.9.0 with no edit, which is why
this is a cap-only patch rather than a behaviour release. The floor stays at
0.8.3 for the `facet` reason recorded in `pyproject.toml`.

## [0.16.0] — 2026-09-03

### Fixed

- **The public catalogue served the sync feed's answer to strangers (Д88).**
  `GET /categories/` returned 174 rows named `smoke-1787331903`,
  `authz-1787369370`, `storefront-…` on a live stand — every acceptance run
  the fleet had ever done, to anyone with curl and no credentials. Two
  contracts had collided on one URL: the flat list is the revision-SYNC feed
  and MUST serve retired rows (a consumer that cannot see a retirement cannot
  apply it), and it is also the catalogue a storefront walks. The sync
  contract won.

  Fixed by splitting the READERS, not by deleting the rows — the rows are
  legitimately inactive and a syncing consumer is legitimately entitled to
  them. A sync principal (a fleet service via `X-API-KEY`, or staff) gets
  exactly what it always got, `include_deleted` included. Everyone else gets
  `visible_categories()`, which the flat list now shares with the three tree
  reads; that it did not was the other half of the defect, since the two
  doors answered differently about the same tree.

### Changed

- **`active` is a visibility gate where hiding the row opens no hole.**
  `visible_categories()` served every inactive row, on the grounds that
  hiding one "would open a hole under the live categories beneath it". True —
  and it was being applied to rows with nothing beneath them, which structure
  nothing. A category is now public iff it is active or an active category
  still hangs from it (`structural_ancestor_ids()`, one column read over the
  active set — django-treenode already denormalises the ancestry — cached on
  the same revision fingerprint the roots cache uses).

  A retired branch therefore leaves as a whole once its last live leaf does,
  and retiring a category finally does what an operator means by it. The
  advice this module used to give — "a deployment that wants test rows hidden
  hides them with `active`" — could not work while `active` was not a gate,
  which is how those 174 rows survived every sweep that took it.

  `is_test` is still not a runtime filter, and is still a field nothing in
  the fleet writes; nothing should be built on it.

  **Upgrade note:** a deployment carrying published content under a retired
  category will find that category gone from the public tree. The content
  stays searchable — `categories.path` and `categories.names` are unfiltered
  and unchanged, so an index keeps its ancestry — but the storefront will not
  offer the category. That is the intended meaning of retiring one; move or
  archive the content, or reactivate the category.

## [0.15.2] — 2026-09-03

### Fixed

- Re-cut of 0.15.1, which never reached PyPI: it raised the `stapel-attributes`
  floor to `>=0.8.3` and its own CI ran in the minutes before 0.8.3 finished
  publishing, so every matrix leg failed to resolve. Identical code. Pin
  0.15.2.

## [0.15.1] — 2026-09-03

Patch (pre-1.0: minor = breaking, patch = compatible). The buyer-facet
opt-out is documented and gated; no behaviour changes.

### Added

- **`config["facet"]` is now a named, gated key.** stapel-search's facet plan
  has always read a `facet` flag off a resolved feature (defaulting to TRUE
  when absent) to decide whether a category offers the feature as a filter
  axis — and nothing in any fleet wrote it, because nothing said it existed.
  A live classified stand's «Аквариум» consequently offered a buyer a filter
  panel made entirely of the delivery block (parcel weight, length, height,
  width), and its phone leaf planned the wholesale ladder as facets. The key
  is an ENGINE-LEVEL config key, not a type's: it is not an input to value
  validation (nothing about the value changes), and the same `int` is a real
  filter one category over, so no type could ever infer it.

  Nothing on the path had to change — `config` is opaque to the FeatureDef
  canon and passes through the loader, the export, the inline-override shape,
  the admin form, the feature editor and `categories.features` verbatim. What
  was missing was anyone knowing that, so this release makes it load-bearing
  rather than accidental: `tests/test_facet_optout.py` is a standing gate over
  all seven of those surfaces (including the export/load idempotency the
  3-way diff needs, and the two REWRITE paths where a typed rebuild would
  silently drop the key and put the delivery weight back in the panel), the
  `categories.features` schema describes it on `config`, and MODULE.md
  documents it beside the disclosure axis. Requires stapel-attributes 0.8.3,
  which allowlists it out of the "unknown config key ignored" warning — that
  warning's contract is "your key was dropped and does nothing", and it was
  telling reviewers to delete a working opt-out.

## [0.15.0] — 2026-09-03

Minor (breaking: `active` leaves the catalogue sync's ownership, and the
sidecar hash changes meaning — `STATE_VERSION` 3 -> 4).

### Fixed — a re-import resurrected categories the operator had retired

0.13.0 pulled the three presentation keys out of the sync because a
catalogue re-import had reset a live stand's home-screen tiles. `active` was
left in, and it bit the same way one field over: an operator deactivated two
untyped leaves and a duplicate sibling in the admin, and the next load —
of records that changed for real reasons of their own — rewrote those rows
wholesale and put all three back in front of sellers.

Whether a category is OFFERED on this stand is curation in exactly the sense
the carousel is, so it gets the same cure rather than a second one:

- `cf.CURATION_KEYS` = `PRESENTATION_KEYS + ("active",)`, and
  `category_sync_view` strips all four, so a deactivation is not a sync
  event on either side — no db-drift warning, no phantom conflict, no
  re-write on every subsequent load;
- `_apply_category_upsert` writes `active` only when it CREATES the row. An
  export→restore of a whole stand still rebuilds its state, inactive rows
  included; an update leaves whatever the operator set.

This costs canon nothing it had. The producer emits `active: true` for every
record and has no way to express retirement through this key at all — a
category that leaves the catalogue leaves the FILE, which is what
`--deletions` is for.

### Added — `catalog_health` also gates on resurrections

A guard protects the one path it sits on. A resurrection arriving another
way — a queryset `.update(active=True)`, a fixture applied by an older
release, a hand edit — leaves nothing for it to catch, so the gate asserts
the SHAPE such a write produces instead of the event.

`catalog_load.active_under_inactive_parent()` returns the slugs of active
categories hanging under an inactive one: reachable by search or a saved
link while the path to them is closed, which no deliberate curation
produces. An operator retires a subtree from the top, and a fully retired
subtree is silent here — the gate names the inconsistent half.

`catalog_health` runs both checks in one pass (two findings beat a gate that
hides the second behind the first), names each row and the parent it hangs
off, and exits non-zero for either. `load_catalog` stamps the same list onto
its report as `resurrected`: a load can no longer cause one, which is
precisely why one showing up there is worth reading.

### Upgrading

Sidecars written by 0.13.x/0.14.x are `version: 3` and are refused loudly
(`incompatible .sync-state.json version`) rather than compared against a
hash that now covers a different subset of the record. Re-run
`export_catalog` to write a v4 sidecar, or load once without one.

## [0.14.0] — 2026-09-02

### Added — `categories.children`: one rung of the cascade, over comm

The tree was walkable over HTTP from 0.12.0 (`roots/`, `children/`,
`by-slug/`) and over comm not at all: `features` resolves a node,
`path` goes up, `suggest` goes by name, `names` captions ids the caller
already holds — nothing listed a node's children. svc-agent walks the
catalogue the way a buyer walks the storefront cascade — "give me the
children of X" — and it walks over comm, so the missing rung is a comm
Function.

```python
call("categories.children", {"parent_id": None})   # null/absent = the roots
# -> {"parent_id": None, "children": [
#      {"id": 46, "slug": "vehicles", "name": "Vehicles",
#       "children_count": 2},
#      … ]}
```

- **Same rungs, same order as the storefront.** Ordering is the tree HTTP
  views' (`-tn_priority`, then `id`), pinned by
  `test_ordering_matches_the_http_tree_view`.
- **Active rungs only, and the counts agree.** Each child carries
  `children_count` — its own number of ACTIVE children, 0 meaning leaf, so
  the walker knows the bottom without a second call. Deliberately narrower
  than the HTTP reads' `visible_categories()` (which keeps inactive rows
  and ships the `active` flag for a client to grey out): this caller is
  choosing where to step next and the rows carry no flag, so an inactive
  category is not a rung at all — on the row, in the counts, and as a
  parent.
- **"No such rung" is not a leaf.** An unknown, inactive or deleted
  `parent_id` raises `LookupError` (the `categories.features` convention);
  a leaf answers `{"children": []}`.
- **Names are display names.** Rendered through the `DISPLAY_TRANSLATOR`
  seam exactly as `categories.names` and `categories.suggest` render
  theirs — a raw key would hand the walker `categories.electronics` as a
  rung caption.
- **Two queries flat** (parent check + one annotated read), whatever the
  width of the rung. Contract committed as
  `schemas/functions/categories.children.json`, validated at the call
  boundary like its siblings.

## [0.13.0] — 2026-09-02

Minor (pre-1.0: minor = breaking, patch = compatible). Five findings from one
event: a live classified stand imported a full external catalogue through
`load_catalog` into a tree that already had hand-seeded categories and
operator curation. Everything worked. Five things were wrong anyway, and each
fix here is a mechanism, not a stand patch.

### The public read surface stops serving provenance (breaking)

Every anonymous category read — list, detail, `children`, `roots`,
`carousel`, `by-slug` — was serializing `external_id` and `external_source`
on every row: the source catalogue's **own node ids**, i.e. a competitor's
internal numbering, readable by anyone with curl. The fields exist so a
re-import can find its rows again; that is an operator fact, and it now lives
only where operators are: the Django admin, the staff bulk serializer, and a
new `CategoryStaffSerializer` served on the staff-gated write actions
(create/update responses included — a staff edit round-trips what it may
set).

This removes two keys from public payloads, hence the minor bump. The public
projection is now a **frozen key set**, asserted exactly
(`tests/test_public_read.py::PUBLIC_CATEGORY_KEYS`): the next leaked field
will not be called external-anything, and an exact contract makes adding a
public key a conscious act rather than a serializer default.

### A re-import can no longer clobber the operator's curation

The stand's operator had curated ten roots onto the home-screen carousel
(`carousel_enabled`, `carousel_icon`, `tn_priority`). The next catalogue
re-sync — any fixture-side change to those records — wrote the fixture's
defaults (`""`, `""`, `False`) over all of it, and the home screen lost its
tiles. The fixture's contract is **taxonomy + features; presentation is the
operator's**, and the code now says so twice:

- `catalog_icon` / `carousel_icon` / `carousel_enabled` are excluded from
  BOTH sides of the 3-way content hash (`catalog_fixtures.category_sync_view`
  is applied to the DB state, the fixture record and — by construction — the
  sidecar base), so a presentation-only difference is not a sync event in
  either direction: no fast-forward, no db-only-drift warning, no phantom
  conflict, no revision churn.
- `_apply_category_upsert` writes the three only when it **creates** the row.
  An export→restore of a whole stand keeps its curation (the fixture files
  still carry the fields); an update leaves whatever the row has, under every
  `--on-conflict` policy. `tn_priority` was already fixture-invisible and is
  now pinned by a test to stay that way.

The sidecar hash semantics changed, so `STATE_VERSION` is bumped to 3 —
a 0.12.x sidecar is refused loudly with "regenerate via export_catalog"
instead of reading every category as a phantom two-sided change.

### A hand row inside a canon subtree is duplicate-shaped — the report says so

The stand ended up with seed children («Smartphones», «Laptops»…) sitting
BESIDE imported canon siblings under an imported root; sellers picked between
near-duplicates and no report line ever distinguished that state from a
deliberately local subtree. Two new machine-readable report kinds:

- `db_new_in_canon` — a live category the fixture does not know whose PARENT
  is fixture-owned. The generic `db_new` ("not in canon") is a legitimate
  steady state for a local root; a hand row parked between imported siblings
  is not, and the warning names both the row and its canon parent.
- `name_collision` — two live, active, non-deleted siblings under one parent
  carrying the same case-folded name (the stand's literal case: two active
  «Другое» under one branch). Diagnosed over the whole live tree after apply,
  and over the current tree in `--dry-run`, because either colliding row may
  be hand-seeded, imported, or years old.

Both warn without failing the load; both print as warning lines in the
command output.

### `catalog_health`: no active dead ends

An ACTIVE leaf category with ZERO features — own or inherited — is a dead
end: a seller can pick it, and it types nothing (no form, no validation, no
facet). The imported catalogue's untyped scraps landed exactly like that and
sellers found them before tooling did. The new `catalog_health` management
command lists every such leaf and exits non-zero when any exist — a CI/deploy
gate, deliberately with **no** allow-flag (attach a feature, deactivate, or
merge; an allowed dead end is still a dead end). The finder resolves features
with `Category.get_all_features` — the library's real inheritance logic — so
the gate cannot disagree with the form the product renders. `load_catalog`
surfaces the same finding at import time (`report.dead_end_leaves` + a
summary line), so the import that creates dead ends says so itself.

### `categories.names`: ids in, captions out

stapel-search 0.9.1's goods-driven suggest rows carry category path IDS and
had no fleet Function to caption them — `categories.path` maps ids to
id-paths and `categories.suggest` matches terms; nothing answered "what is
163 called?". New comm Function `categories.names`: `{"ids": [163, "149"]}`
→ `{"names": {"163": {"name": …, "slug": …}}}`. Keys are ids as strings on
both sides of the wire (a JSON round trip must not change key types — the
`categories.path` rule); deleted rows and unknown ids are absent (a stale id
degrades to no caption, not an error); inactive rows still answer, because a
listing can sit in a category retired after publication; names render through
the `DISPLAY_TRANSLATOR` seam exactly as `suggest` renders them. The batch is
schema-capped at 200.

## [0.12.2] — 2026-09-02

Patch. Reverts 0.12.1's floor, and corrects what 0.12.1 said.

**0.12.1's changelog was wrong.** It claimed stapel-core 0.51.0–0.53.0 shipped
wheels missing `stapel_core.django.sites` and raised the floor to `>=0.54.1`
to exclude them. The published wheels were checked afterwards and all of them
contain the module. Nothing on PyPI was ever broken, and this floor had no
reason to move; it goes back to `>=0.26.0`.

What really happened: core's main briefly carried a `pyproject.toml` whose
`[tool.setuptools] packages` list had lost the `stapel_core.django.sites`
line to a rebase conflict resolution. It was tagged as core 0.54.0, caught by
core's own CI, never published, and fixed in 0.54.1. Siblings whose CI builds
core from **git main** rather than PyPI failed at `django.setup()` while it
was there. That is a real failure with a real cause, and it is not this one.

The diagnosis jumped from "a wheel is missing a module" to "the published
wheels are missing a module" without checking a published wheel. The check
takes one `pip download`.

## [0.12.1] — 2026-09-02

Patch. Raised `stapel-core` to `>=0.54.1`. **Superseded by 0.12.2 — its stated
reason was incorrect; see that entry.**

## [0.12.0] — 2026-09-02

Minor (pre-1.0: minor = breaking, patch = compatible). Two new public
read actions, one new setting, and one filter that stopped being written out
three times.

### The two rungs the server could not answer

A storefront walks the tree from the top: show the root tiles, resolve the
slug in the URL, list that category's children. The third had an endpoint
from the first release. The first two did not.

So a client that wanted "what are the top-level categories?" or "which
category is `/c/electronics`?" had exactly one way to ask: **list the whole
table and filter it client-side.** On this fleet that is a 15-page walk —
614 KB of JSON, and the single most-requested route on the stand by a factor
of three — to render a row of tiles or to resolve one slug. The cold `/c`
page measured **21 seconds**. These two actions are what kill it: a root
listing is one small response, and a slug is one row.

`children` having always existed is what made the gap easy to miss — the tree
was walkable from the second rung on, and only the first had no door.

### `GET categories/roots/`

Top-level categories (`tn_parent IS NULL`), `-tn_priority` then `id`,
unpaginated (a catalogue's roots are tens of rows, not thousands), public.

Cached server-side under a key fingerprinted by the tree's revision state —
`(max revision, row count)`, the same mechanism `categories.suggest` uses for
its folded-name index. A TTL alone would be the `categories_carousel`
bargain: an edit stays invisible until the clock runs out. Here the key
itself changes when the tree does, so a mutation retires the entry
immediately and `TREE_CACHE_TIMEOUT` is only the ceiling on how long an
*unchanged* tree keeps one. Both halves of the fingerprint are needed:
`revision` alone misses a pure deletion, the row count alone misses an edit
that keeps the count the same.

### `GET categories/by-slug/<slug>/`

Resolves a slug to one category; 404 with `error.404.categories_slug_not_found`
otherwise.

A path segment and **not** a `?slug=` filter, deliberately: `slug` is
`unique=True`, so this resolves an alternate primary key and returns an
object, not a list of at most one. A query parameter would have made every
caller unwrap a collection to say "get this category", and would have implied
a filter contract (repeated values, partial matches) that a unique key does
not have. The numeric detail route is untouched and pinned by a test.

### One visibility rule, in one function

`views.visible_categories()` is now the single definition of what the public
tree shows, called by all three reads. Two of these endpoints are new, and
the way this drifts is somebody copying the filter out of `children` and then
changing one of the copies. It is asserted by comparing the endpoints against
**each other** rather than by restating the filter in the tests, because a
restated filter is a fourth copy that can drift too.

What it filters, and what it deliberately does not — this is `children`'s
pre-existing contract, now stated rather than implied:

- `deleted=False`. A soft-deleted category is gone to any reader;
  `deleted-children` is the staff view that asks for them on purpose.
- **`active` is not filtered.** An inactive category still occupies a place
  in the tree, and hiding it would open a hole under the live categories
  beneath it. The serializer ships `active` on every row, so a client that
  wants to grey one out can.
- **`is_test` is not filtered.** Per the model's own declaration, it is an
  *export* filter — `export_catalog` excludes such rows from committed
  fixtures — not a runtime-visibility gate. A deployment that wants test rows
  hidden from the storefront hides them with `active`.

Both are pinned by tests, so making either a visibility gate later is one
edit in one function and a deliberate one.

### Added

- `STAPEL_CATEGORIES["TREE_CACHE_TIMEOUT"]` (default `300`).
- `Cache-Control: public, max-age=<TREE_CACHE_TIMEOUT>` on all three tree
  reads. `children` had none before, so an edge cache applied whatever
  default it liked to the fleet's hottest navigation read.
- `error.404.categories_slug_not_found`.
- `children` now sorts `-tn_priority, id`. It sorted on `-tn_priority` alone,
  which left equal-priority siblings in DB-arbitrary order — a tile grid that
  could reshuffle between two identical requests.

## [0.11.0] — 2026-09-02

Minor (pre-1.0: minor = breaking, patch = compatible). One new column on
`Feature`, one migration, and one widened comm contract: a feature can now say
**who is allowed to read its stored values**.

### Added

- **`Feature.visibility` — `public` (default) / `owner` / `staff`.** Some
  attributes do not describe an object, they *identify* one: a VIN, an IMEI, a
  serial number, a registry number. Knowing the value lets a stranger act as
  that unit's owner — order duplicate keys against the VIN, clone a handset's
  identity from its IMEI. They are legitimate catalogue fields (a marketplace
  wants them mandatory, validated and deduplicated on) that must never be
  printed on a public page. This column is the one place that decision is
  recorded, and until it existed the axis stapel-attributes 0.8.0 added could
  not be set on a single real feature.

  It is **orthogonal to `mandatory`**: a non-public feature is still required,
  still validated against its config, still stored verbatim, still visible to
  moderation and still editable by its owner. It is only never handed to a
  reader who is not entitled to it — and that hiding happens downstream, in
  stapel-listings, off the stamp the attribute engine writes into each stored
  value. What this module owns is the decision.

  `public` is the default, so **nothing that existed before this release
  changed**: every current row is public, every fixture that says nothing
  loads as public, and every payload that omits the key reads as public.

- **`visibility` crosses every feature-carrying boundary**, because a dropped
  disclosure decision does not fail — it answers `public`, which is exactly
  the publication the axis exists to prevent. It is now in
  `Category.feature_defs()` and `get_feature_schema()`, in `FeatureSerializer`
  / `FeatureCompactSerializer` / the writable `FeatureEditorFeatureSerializer`
  (and the editor's create/edit/inherit apply paths), in the catalog fixture
  export and loader, and in the `categories.features` comm response, where
  `$defs.ResolvedFeature` now **requires** it —
  `tests/test_resolved_feature_contract.py` gates that against the
  `FeatureDef` canon.

- **The admin grew a "Disclosure" fieldset**, deliberately NOT a row inside
  "Display Options": this is not a display flag, and sitting it beside the
  badge checkbox invites someone to flip it while tidying a form. Its help
  text says what the setting does and does not do, and names the re-projection
  below.

### Changed

- **A non-public feature is never a title and never a badge.**
  `show_at_title` and `show_as_badge` are forced to `False` in both
  `Feature.clean()` and `Feature.save()` — the second because nothing except
  the admin calls `full_clean()`, so the feature editor, the catalog loader
  and every fixture reach the table through `save()`. `FeatureDef` resolves
  the same contradiction downstream, but resolving it downstream leaves the
  contradictory ROW in the table for the next reader to resolve again. An
  unrecognized visibility raises instead of downgrading: a typo like
  `"private"` must not quietly publish a VIN.

- **Requires stapel-attributes >= 0.8, < 0.9** — the floor moves this time.
  `stapel_attributes.visibility` does not exist before 0.8.0, and the
  committed comm schema promises a `visibility` on every resolved feature, a
  promise this module can only keep against a `FeatureDef` that has the field.
  A host still on 0.6/0.7 stays on stapel-categories 0.10.0.

### Upgrading

1. Run the migration (`0005_feature_visibility`) — one `AddField` with a
   `public` default, no data rewrite.
2. Nothing else is required. A deployment that never sets a non-public
   visibility behaves exactly as it did.
3. **Setting the axis on a feature is not finished when you save the
   feature.** The stamp travels with the value and is written at projection
   time, so values already stored still carry no stamp and still read as
   public. Re-stamp them:

   ```
   python manage.py listings_reproject_features --category <id>
   ```

   Until that runs, the new setting applies to values written from now on and
   to nothing already in the table.

## [0.10.0] — 2026-09-02

Minor (pre-1.0: minor = breaking, patch = compatible). One comm-surface
change: `categories.suggest` grades a match four ways where it graded it two,
and the provider's own result cap keeps by the grade.

### Changed

- **`Suggestion.match` is now `exact` / `prefix` / `word` / `substring`**,
  best first, and the enum's ORDER is part of the contract — the caller ranks
  on it (`stapel-search` 0.8) and the cap below keeps by it. The two values it
  replaces still exist and mean what they meant; what is new is that an exact
  name is told apart from a prefix, and a hit at the START OF A WORD inside a
  name is told apart from one buried mid-word.

  Measured on a 3583-node catalogue: transliterating «iphone» yields «ифон»,
  which occurs inside «Сифоны» and inside nothing else on the board, so the
  single suggestion a buyer typing «iphone» received was a plumbing trap. A
  word-boundary hit («Брюки и **шорты**») and a mid-word one («С**ифон**ы»)
  are not the same evidence, and only the module that owns the names can tell
  them apart — which is why the grading lives here and the ranking lives in
  the caller, which has the listing counts.

  `SUGGEST_MATCH_KINDS` states the order once, and `match_kind()` is public
  for a host that grades its own names the same way.

- **The result cap keeps the best matches, not the shallowest.** The cap
  (`_SUGGEST_MAX_RESULTS`, and the caller's `limit`) used to sort candidates
  by depth and id, so a deep exact hit could be dropped before the caller —
  which does the ranking — ever saw it, while three nodes that merely contain
  the word survived. The sort is now match grade, then depth, then id: still
  fully deterministic, and now deterministic about the right thing.

### Upgrading

Nothing to do for a consumer that treats `match` as an opaque label or passes
it through. A consumer that branches on the two old values keeps working —
`prefix` and `substring` still occur — but will not see `exact` or `word` as
the better evidence they are; upgrade it to sort by the enum's order.

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
