## What this is

A hierarchical **category** tree (django-treenode) and a parallel **feature**
tree whose typed `config` is validated by
[stapel-attributes](https://github.com/usestapel/stapel-attributes). Categories
own the tree structure, feature inheritance, the ordered category↔feature M2M,
and the feature-editor lifecycle; the attribute *engine* (types, config/DTO/DAO
validation, polymorphic serializers, admin widgets) lives in stapel-attributes
and is imported, never re-implemented.

## Quick start

```python
INSTALLED_APPS = [
    # ...
    "treenode",            # django-treenode (tree-cache signals)
    "stapel_categories",
]

# urls.py — this module's own urls.py bakes in only `v1/`; the host
# contributes `api/`, giving the canonical `/categories/api/v1/...` prefix
# (exactly what stapel-example-monolith already does for this module).
path("categories/api/", include("stapel_categories.urls"))
```

`stapel-attributes` is an imported library (no app to install); its config
editor ships static assets, so run `collectstatic` if you use the admin.

## Presenting the tree

Every node says how its **children** should be drawn, as `children_as` on
every public read:

| value | meaning |
|---|---|
| `tiles` | the children are real subcategories — a tile grid of destinations |
| `chips` | the children partition ONE attribute template (new/used, buy/sell/rent, boys/girls) — a chip row over the parent's own feed |
| `transparent` | browsing SKIPS this node: its children appear where it would, and its own page is its parent's |
| `null` | the node has no children |

`transparent` is the import wrapper — a level a catalogue keeps for placement
that nobody should have to browse through («Предложение услуг» between a root
and the 34 groups that are the real level). The TREE is unchanged: the node
keeps its id, its path and its place as the target of a listing; only the
presentation of it is. It is AUTHORED only — the collapse is an editorial call
read off a census, and no signal on a tree can make it, so `derive_children_as`
never emits it and never overwrites it.

A `chips` **or `transparent`** parent that declares no features of its own
answers the **effective
schema** — the intersection of its children's — wherever features are read
(`GET /categories/<id>/features/`, the `categories.features` Function). It
renders the feed and the chip row for the whole partition, so "what can be
filtered here" is a question about the children. A feature only SOME children
carry is not in it (it appears when its chip is picked); a feature the children
disagree on carries `divergent: true` beside the WIDEST config of theirs, so a
client may render it (it refuses nothing a child accepts) or hide it until a
chip is picked. The HTTP read says which it did in the `X-Effective-From`
header (`own` / `children`), the Function in `effective_from`. A parent with
features of its own keeps them ALONE — own only, never own plus the
intersection. A `transparent` node has no page of its own, but a composer
walking through it and every caller of `categories.features` still ask what it
types, and a wrapper's own links are empty by construction — so it takes the
same rule. That is the whole of the overlap: it draws no chip row and gets no
axis caption.

A chip row also needs a NAME for the axis it splits on — «Все | С пробегом |
Новые» is a set of values, and only the parent can say what they are values
of. That name is `children_axis_label`, an optional translation key on the
parent (empty means the row is drawn uncaptioned), authored in the admin or
over the staff serializer and carried on every public read next to
`children_as`.

The stored column takes a third value, `auto`, which a reader never sees:
`auto` means "nobody has decided", and it is resolved server-side. Two
columns hold the two answers — `children_as` is the authored intent,
`children_as_derived` is the derivation's cache — so a re-run can improve its
own output without ever overwriting an operator's.

```bash
django-admin derive_children_as              # report only
django-admin derive_children_as --apply      # write the derived column
```

An `--apply` run also NAMES the axis of the rows it makes chips, from the
vocabulary group it already matched (deal type, condition, for whom) — a
translation key per group, never a rendered word. Authored text always wins,
and the command only ever replaces a label it emitted itself, which is what
keeps the step re-runnable.

The command prints one line per parent with the decision, the signal that
carried it (`schema`, `vocabulary`, `empty-schema`, `structure`,
`vocabulary>structure`) and the Jaccard overlap of the children's own feature
keys, so a wrong call can be pinned by hand — set `children_as` to `tiles` or
`chips` in the admin and no future run touches it. A node already authored
(including `transparent`) is printed with its value and skipped.

A census is applied without the admin, in one command, idempotently:

```bash
django-admin set_children_as --path uslugi/predlozhenie-uslug --value transparent
django-admin set_children_as --paths-from census.txt --value transparent
django-admin set_children_as --path a/b --value tiles --dry-run
```

The path is the slug path root→self — the exact form the derivation report
prints, so a census read off that report pastes straight back. A bare slug
works too (the column is unique); a longer path is checked against the tree and
refused if it no longer matches. Every path resolves before any is written, so
one bad line leaves nothing half-applied, and a node that already carries the
value is reported `unchanged` and not re-saved.

A child set that spells a partition is a chip row even where some of those
children have children of their own: `Квартиры` → `Продам`/`Сдам`/`Куплю`/
`Сниму` is a partition whether or not `Продам` splits further, and each such
child is then a parent whose own children are decided by the same rules.
`structure` stays a veto only where the names say nothing.

`children_as` travels in the catalogue fixture (`export_catalog` /
`load_catalog`), so an authored decision survives an image rebuild; the
derived cache does not — a load leaves it to be re-derived.

The whole visible tree comes back nested in one cached call:

```
GET /categories/api/v1/tree/?depth=3     # 1..4, default 3
```

Active nodes, ordered by `tn_priority` descending at every level, carrying
`id`, `slug`, `name`, `path` (the `/`-joined id path a search query takes),
`catalog_icon`, `children_as`, `children_axis_label` and `children`. One query
whatever the depth.

## Settings

All configuration lives in the `STAPEL_CATEGORIES` namespace (dict setting,
flat setting, or env var — resolved lazily):

| Key | Default | Meaning |
|---|---|---|
| `CAROUSEL_CACHE_TIMEOUT` | `300` | Seconds the `carousel` response is cached. |
| `FEATURE_DISPLAY_CACHE_TIMEOUT` | `60` | Seconds an admin feature display label is memoized. |
| `DISPLAY_TRANSLATOR` | `stapel_categories.translation.identity_translator` | Dotted path `(key)->str` for rendering translation keys (default: identity). |

## comm surface

| Kind | Name | Contract |
|---|---|---|
| Function | `categories.features` | `{"category_id": int}` -> `{"category_id", "revision", "features":[{id,slug,name,mandatory,config}]}` — resolved schema (own + inherited), cacheable by `revision` |
| Function | `categories.path` | `{"category_ids": [int, ...]}` -> `{"<id>": ["<root_id>", ..., "<id>"]}` — root->leaf ancestry, one query for the batch; segments are ids, an unknown id is absent |
| Function | `categories.by_slug` | `{"slugs": ["transport", ...]}` -> `{"<slug>": ["<root_id>", ..., "<id>"]}` — the same ancestry keyed by slug (`Category.slug` is globally unique); an unknown slug is absent, an inactive node still answers |
| Action (emit) | `category.changed` | `{"category_id": int, "revision": int}` on any category/feature mutation — for downstream cache invalidation |

`categories.features` lets stapel-listings validate attribute values against a
category's schema without importing this module. `categories.path` is the
provider stapel-search declares by canonical name for category rollup — without
it a search index degrades to a single path segment and a filter on a parent
category finds none of its descendants. `categories.by_slug` is the same
answer in the other namespace: it is what lets a page addressed
`/c/avtomobili` ask for its own feed, and without it every slug segment of a
search query degrades instead of filtering.

## Contract

`docs/{schema,flows,errors}.json` are emitted from a single-module
`{categories + core}` Django instance mounted at the canonical
`/categories/api/v1` prefix (`make contract` / `make contract-check`; see
`_codegen.py`) — the same mechanism stapel-search, stapel-chat and
stapel-forms already use. `docs/flows.json` is `[]`: no flow is declared via
`@flow` yet, same state as every other contract-complete module today.
`docs/capabilities.json` stays hand-authored (see the Makefile comment); only
its `surface` section is derived.

The ten feature-value shapes (`FeatureConfig`, `FeatureDto`) are a proper
discriminated `oneOf` keyed by `type`, contributed by stapel-attributes and
now used consistently everywhere a config or a values-DTO crosses the wire —
`Feature.config`, `FeatureBulk.config`, the `convert-type` request body and
`validate-dto`'s `features` field all resolve through it (previously the
last three fell back to an untyped `JSONField`/`DictField`).

**Delta note — one pair of fields stays untyped, and it isn't this module's
to fix.** `FeatureValidationResult.id` / `.ref_value` render as free-form in
the schema. Both are defined by **stapel-attributes**
(`FeatureValidationResult` dataclass + its `serializers.JSONField`
projection in `results.py`), not by this module: `id` is
`Optional[Union[int, str]]` and `ref_value` is
`Optional[Union[str, int, float, list]]` — plain scalar unions with no
`type` discriminator to key a `oneOf` on, unlike the ten-way `config`/DTO
shapes. Typing them is upstream's serializer to extend, not a gap
`stapel-categories` introduced or can close by itself.

## Extension points

See [MODULE.md](https://github.com/usestapel/stapel-categories/blob/main/MODULE.md) — the agent-facing map of every fork-free seam
(settings, serializer seams, comm surface, feature-editor actions, admin-UI
pointer to stapel-attributes).

## Development

```bash
pip install -e . && pip install pytest pytest-django ruff
./setup-hooks.sh
pytest tests/
```
