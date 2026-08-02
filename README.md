# stapel-categories

[![CI](https://img.shields.io/github/actions/workflow/status/usestapel/stapel-categories/ci.yml?branch=main&logo=github&label=CI)](https://github.com/usestapel/stapel-categories/actions/workflows/ci.yml?query=branch%3Amain)
[![coverage](https://img.shields.io/codecov/c/github/usestapel/stapel-categories?branch=main&logo=codecov&label=coverage)](https://app.codecov.io/gh/usestapel/stapel-categories)
[![pypi](https://img.shields.io/pypi/v/stapel-categories?logo=pypi&logoColor=white&label=pypi)](https://pypi.org/project/stapel-categories/)
[![downloads](https://static.pepy.tech/badge/stapel-categories/month)](https://pepy.tech/project/stapel-categories)
[![python](https://img.shields.io/pypi/pyversions/stapel-categories?logo=python&logoColor=white)](https://pypi.org/project/stapel-categories/)
[![license](https://img.shields.io/github/license/usestapel/stapel-categories)](https://github.com/usestapel/stapel-categories/blob/main/LICENSE)
[![llms.txt](https://img.shields.io/badge/llms.txt-blue)](https://github.com/usestapel/stapel-categories/blob/main/docs/llms.txt)

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
