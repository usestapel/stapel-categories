"""Test mounts for this module.

Two, deliberately, and the ORDER matters.

`/catalog/api/` is the prefix the ported legacy-catalog suite addresses, and
every existing test here is written against it. `/categories/api/` is the
prefix the CONTRACT is emitted at (`codegen_urls.py`) and the one every path
in the committed `docs/schema.json` carries — without it, not one of those
paths resolved under this urlconf, so nothing in this repository had ever
driven the document it ships.

The contract mount comes FIRST because `reverse()` with two mounts answers
with the LAST registered match, and this module has a view that builds a
redirect `Location` through it. Contract first means `reverse()` keeps
answering with the prefix the existing tests assert, while both prefixes
resolve. In production there is only ever one mount — the host's — so this
ambiguity is a property of the test harness, not of the module.

`tests/test_contract_wire.py` declares the contract mount itself and drives
the committed document against it.
"""
from django.urls import include, path

urlpatterns = [
    path("categories/api/", include("stapel_categories.urls")),
    path("catalog/api/", include("stapel_categories.urls_v1")),
]
