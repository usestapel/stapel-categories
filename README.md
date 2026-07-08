# stapel-categories

[![CI](https://github.com/usestapel/stapel-categories/actions/workflows/ci.yml/badge.svg)](https://github.com/usestapel/stapel-categories/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/usestapel/stapel-categories/graph/badge.svg)](https://codecov.io/gh/usestapel/stapel-categories)

Category tree with typed features for the [Stapel framework](https://github.com/usestapel) —
composable Django apps that deploy as a monolith or as microservices without
changing module code.

A hierarchical **category** tree (django-treenode) and a parallel **feature**
tree whose typed `config` is validated by
[stapel-attributes](https://github.com/usestapel/stapel-attributes). Categories
own the tree structure, feature inheritance, the ordered category↔feature M2M,
and the feature-editor lifecycle; the attribute *engine* (types, config/DTO/DAO
validation, polymorphic serializers, admin widgets) lives in stapel-attributes
and is imported, never re-implemented.

## Install

```bash
pip install stapel-categories
```

```python
INSTALLED_APPS = [
    # ...
    "treenode",            # django-treenode (tree-cache signals)
    "stapel_categories",
]

# urls.py — the host chooses the prefix
path("categories/", include("stapel_categories.urls"))
```

`stapel-attributes` is an imported library (no app to install); its config
editor ships static assets, so run `collectstatic` if you use the admin.

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
| Action (emit) | `category.changed` | `{"category_id": int, "revision": int}` on any category/feature mutation — for downstream cache invalidation |

`categories.features` lets stapel-listings validate attribute values against a
category's schema without importing this module.

## Extension points

See [MODULE.md](MODULE.md) — the agent-facing map of every fork-free seam
(settings, serializer seams, comm surface, feature-editor actions, admin-UI
pointer to stapel-attributes).

## Development

```bash
pip install -e . && pip install pytest pytest-django ruff
./setup-hooks.sh
pytest tests/
```

## License

MIT
