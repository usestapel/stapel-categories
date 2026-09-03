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
| `null` | the node has no children |

The stored column takes a third value, `auto`, which a reader never sees:
`auto` means "nobody has decided", and it is resolved server-side. Two
columns hold the two answers — `children_as` is the authored intent,
`children_as_derived` is the derivation's cache — so a re-run can improve its
own output without ever overwriting an operator's.

```bash
django-admin derive_children_as              # report only
django-admin derive_children_as --apply      # write the derived column
```

The command prints one line per parent with the decision, the signal that
carried it (`schema`, `vocabulary`, `empty-schema`, `structure`) and the
Jaccard overlap of the children's own feature keys, so a wrong call can be
pinned by hand — set `children_as` to `tiles` or `chips` in the admin and no
future run touches it.

The whole visible tree comes back nested in one cached call:

```
GET /categories/api/v1/tree/?depth=3     # 1..4, default 3
```

Active nodes, ordered by `tn_priority` descending at every level, carrying
`id`, `slug`, `name`, `path` (the `/`-joined id path a search query takes),
`catalog_icon`, `children_as` and `children`. One query whatever the depth.

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
| Action (emit) | `category.changed` | `{"category_id": int, "revision": int}` on any category/feature mutation — for downstream cache invalidation |

`categories.features` lets stapel-listings validate attribute values against a
category's schema without importing this module. `categories.path` is the
provider stapel-search declares by canonical name for category rollup — without
it a search index degrades to a single path segment and a filter on a parent
category finds none of its descendants.

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
