"""Every response body the contract declares is a body the views actually send.

``docs/schema.json`` is emitted from the views' ``@extend_schema``
annotations, and an annotation is a CLAIM: it says what the view returns, and
the generator has no way to check it against the method body.
``tests/test_contract.py`` compares the committed document against a FRESH
EMISSION of the same annotations — it proves the file is not stale, and
nothing else, because both sides come from the claim.

This is the gate the generator cannot be: it performs every operation the
committed schema declares with a JSON response body, and validates the body it
gets against the schema it was promised.

Rules this file holds itself to:

* an operation with a declared JSON response and no entry in ``RECIPES``
  FAILS LOUDLY — a gate that quietly covers three of four rows is the family
  of green that proves nothing;
* a path parameter the gate cannot fill fails at the point of substitution,
  naming the operation;
* an operation that genuinely cannot run in-process is listed by name in
  ``UNDRIVABLE`` with a one-line reason. That list is asserted to be exactly
  current: a stale entry, or a missing reason, fails;
* a collection that comes back empty fails — an empty array validates against
  any item schema, so an empty answer is a check that looked at nothing.

Runs on every interpreter: it reads the committed schema and never emits.

The urlconf below is the EMISSION mount (``codegen_urls.py``):
``categories/api/`` + the module's own ``v1/``, giving the canonical
``/categories/api/v1/…`` prefix the document is written against.
``tests/urls.py`` mounts ``urls_v1`` under ``catalog/api/`` — a different
prefix and one segment short — so no path in the committed document resolves
under it.

What it found on its first run: 34 of 34 operations driven, 8 red, in four
families — a required ``divergent`` the wire never sends, ``data.json``
declaring one object and answering an array, ``updated_ids`` declared as
UUID strings and answered as integer pks, and the feature write serializer
answering where ``Feature`` was declared. All four are closed: the last two
by stapel-core 0.71.0, which makes ``RevisionViewSetMixin.data_json``
declare the array it sends and ``BulkUpdateResponse.updated_ids`` a pk
union. ``KNOWN_MISMATCHES`` is empty, and every driven operation answers
the body the contract declares.
"""
import copy
import json
import re
import uuid
from pathlib import Path

import jsonschema
import pytest
from django.contrib.auth import get_user_model
from django.urls import include, path as url_path
from rest_framework.test import APIClient

REPO = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((REPO / "docs" / "schema.json").read_text())

#: The mount the contract is emitted at, reproduced for the test client.
urlpatterns = [
    url_path("categories/api/", include("stapel_categories.urls")),
]

pytestmark = [pytest.mark.django_db, pytest.mark.urls(__name__)]

V1 = "/categories/api/v1"

#: Matches ``SERVICE_API_KEY`` in the suite's settings (conftest.py), which is
#: what ``ServiceAPIKeyMiddleware`` compares ``X-API-KEY`` against.
SERVICE_KEY = "test-service-key"


# ─────────────────────────────────────────────────────────────────────────────
# The contract side: what the document declares
# ─────────────────────────────────────────────────────────────────────────────


def _undiscriminated_union(node):
    """A ``oneOf`` of ``$ref`` branches with nothing to tell them apart.

    ``PolymorphicProxySerializer(resource_type_field_name=None)`` — the
    ``CategoryChild`` union — emits alternatives that OVERLAP by
    construction: ``CategoryLinkedChild`` is ``Category`` plus ``linked``, so
    a pointer satisfies both branches and an exclusive ``oneOf`` rejects a
    body the document plainly describes. That is a generator idiom colliding
    with JSON Schema's exclusivity, not a claim the wire breaks, so such a
    union is read as an alternation.

    A union that DOES carry a ``discriminator`` (``FeatureConfig`` and
    friends, whose branches pin ``type`` to disjoint single-value enums) is
    left exclusive — there the branches are genuinely distinguishable and an
    overlap would be a real finding.
    """
    branches = node.get("oneOf")
    if not isinstance(branches, list) or "discriminator" in node:
        return False
    return all(isinstance(b, dict) and set(b) == {"$ref"} for b in branches)


def _json_schema(node):
    """OpenAPI 3.0 → JSON Schema, for the divergences that matter here.

    OAS 3.0 spells "may be null" as ``nullable: true`` beside a ``type``;
    JSON Schema has no such keyword and would refuse the null. The second
    conversion is the undiscriminated union above. Everything else
    drf-spectacular emits (``$ref``, ``allOf``, ``enum``, ``required``,
    ``readOnly``, ``discriminator``) is JSON Schema as written, or inert.
    """
    if isinstance(node, list):
        return [_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    rebuilt = {k: _json_schema(v) for k, v in node.items() if k != "nullable"}
    if _undiscriminated_union(rebuilt):
        rebuilt["anyOf"] = rebuilt.pop("oneOf")
    if node.get("nullable"):
        return {"anyOf": [rebuilt, {"type": "null"}]}
    return rebuilt


def _validator(response_schema):
    root = copy.deepcopy(response_schema)
    root["components"] = copy.deepcopy(SCHEMA["components"])
    return jsonschema.Draft202012Validator(_json_schema(root))


def _operations():
    """Every ``(method, path, 2xx code, JSON body schema)`` the contract declares."""
    ops = []
    for path, methods in SCHEMA["paths"].items():
        for method, op in methods.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            for code, response in op.get("responses", {}).items():
                body = (
                    response.get("content", {})
                    .get("application/json", {})
                    .get("schema")
                )
                if body is not None and code.startswith("2"):
                    ops.append((method.upper(), path, int(code), body))
    return sorted(ops, key=lambda o: (o[1], o[0]))


OPERATIONS = _operations()


# ─────────────────────────────────────────────────────────────────────────────
# The wire side: harness
# ─────────────────────────────────────────────────────────────────────────────


def _unique(prefix):
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def anonymous():
    return APIClient()


def staff_client():
    """The operator side of ``ReadOnlyOrStaff`` / ``IsStaffUser``."""
    user = get_user_model().objects.create(
        username=_unique("wire_ops_"),
        email=f"{_unique('wire-')}@example.com",
        is_staff=True,
        is_superuser=True,
    )
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def make_category(**kwargs):
    from stapel_categories.models import Category

    slug = kwargs.pop("slug", None) or _unique("wire-cat-")
    defaults = dict(name="Wire Category", slug=slug)
    defaults.update(kwargs)
    return Category.objects.create(**defaults)


STRING_CONFIG = {"type": "string", "maxLength": 64}
SELECT_CONFIG = {
    "type": "select",
    "options": [{"value": "new", "label": "feature.state.new"}],
}


def make_feature(**kwargs):
    from stapel_categories.models import Feature

    slug = kwargs.pop("slug", None) or _unique("wire-feat-")
    defaults = dict(name="Wire Feature", slug=slug, config=dict(STRING_CONFIG))
    defaults.update(kwargs)
    return Feature.objects.create(**defaults)


def attach(category, feature, order=0):
    from stapel_categories.models import CategoryFeature

    return CategoryFeature.objects.create(
        category=category, feature=feature, order=order
    )


def category_with_feature():
    """A category carrying one feature — the shape every schema read needs."""
    category = make_category()
    feature = make_feature()
    attach(category, feature)
    return category, feature


def parent_with_child():
    parent = make_category(name="Wire Parent")
    child = make_category(name="Wire Child", tn_parent=parent, tn_priority=5)
    return parent, child


# ─────────────────────────────────────────────────────────────────────────────
# The recipe table
# ─────────────────────────────────────────────────────────────────────────────


class Call:
    """Performs one declared operation, and refuses to guess a path parameter."""

    def __init__(self, method, path):
        self.method = method
        self.path = path

    def __call__(self, client, params=None, data=None, query="", **extra):
        url = self.path
        for name, value in (params or {}).items():
            url = url.replace("{%s}" % name, str(value))
        assert "{" not in url, (
            f"{self.method} {self.path}: a path parameter this gate does not "
            "know how to fill — teach its recipe, or the operation goes unchecked"
        )
        send = getattr(client, self.method.lower())
        if self.method in ("GET", "DELETE"):
            return send(url + query, **extra)
        return send(url + query, data if data is not None else {}, format="json", **extra)


#: How to perform each operation the contract declares with a JSON response
#: body, keyed by ``(METHOD, path template)``. Each recipe receives a ``Call``
#: bound to that operation and returns the response it produced.
RECIPES = {}


def recipe(method, path):
    def register(fn):
        key = (method, V1 + path)
        assert key not in RECIPES, f"duplicate recipe for {method} {path}"
        RECIPES[key] = fn
        return fn

    return register


#: Operations that cannot be driven in-process, by name and with the reason.
#: A short, visible list is acceptable here; a silent skip is not.
#:
#: EMPTY: every operation this contract declares with a JSON body runs
#: in-process. The mechanism stays because the next surface may need it.
UNDRIVABLE: dict = {}


# ── categories: the model surface ────────────────────────────────────────────


@recipe("GET", "/categories/")
def _categories_list(call):
    make_category()
    return call(anonymous())


@recipe("POST", "/categories/")
def _categories_create(call):
    return call(
        staff_client(),
        data={"name": "Created by the wire gate", "slug": _unique("wire-new-")},
    )


@recipe("GET", "/categories/{id}/")
def _categories_retrieve(call):
    return call(anonymous(), params={"id": make_category().pk})


@recipe("PUT", "/categories/{id}/")
def _categories_update(call):
    category = make_category()
    return call(
        staff_client(),
        params={"id": category.pk},
        data={"name": "Replaced by the wire gate", "slug": category.slug},
    )


@recipe("PATCH", "/categories/{id}/")
def _categories_partial_update(call):
    return call(
        staff_client(),
        params={"id": make_category().pk},
        data={"name": "Patched by the wire gate"},
    )


# ── categories: tree reads ───────────────────────────────────────────────────


@recipe("GET", "/categories/{id}/children/")
def _categories_children(call):
    """Both drivable branches of the ``CategoryChild`` union in one body.

    A real child and a POINTER to another branch, so the union is checked
    against more than its first alternative.
    """
    from stapel_categories.models import CategoryLink

    parent, _child = parent_with_child()
    CategoryLink.objects.create(
        source=parent, target=make_category(name="Pointed at"), order=1
    )
    return call(anonymous(), params={"id": parent.pk})


@recipe("GET", "/categories/{id}/deleted-children/")
def _categories_deleted_children(call):
    parent, child = parent_with_child()
    child.deleted = True
    child.save()
    return call(staff_client(), params={"id": parent.pk})


@recipe("GET", "/categories/roots/")
def _categories_roots(call):
    make_category()
    return call(anonymous())


@recipe("GET", "/categories/carousel/")
def _categories_carousel(call):
    make_category(carousel_enabled=True, active=True)
    return call(anonymous())


@recipe("GET", "/categories/by-slug/{slug}/")
def _categories_by_slug(call):
    return call(anonymous(), params={"slug": make_category().slug})


@recipe("GET", "/tree/")
def _tree(call):
    parent_with_child()
    return call(anonymous())


# ── categories: sync feed ────────────────────────────────────────────────────


@recipe("GET", "/categories/revision/")
def _categories_revision(call):
    make_category()
    return call(anonymous())


@recipe("GET", "/categories/data.json/")
def _categories_data_json(call):
    make_category(active=True)
    return call(anonymous(), query="?revision=1")


# ── categories: features and validation ──────────────────────────────────────


@recipe("GET", "/categories/{id}/features/")
def _categories_features(call):
    category, _feature = category_with_feature()
    return call(anonymous(), params={"id": category.pk})


@recipe("GET", "/categories/{id}/validate-configs/")
def _categories_validate_configs(call):
    category, _feature = category_with_feature()
    return call(anonymous(), params={"id": category.pk})


@recipe("POST", "/categories/{id}/validate-dto/")
def _categories_validate_dto(call):
    # A POST, so ``ReadOnlyOrStaff`` refuses the anonymous side even though
    # this action only validates and writes nothing.
    category, feature = category_with_feature()
    return call(
        staff_client(),
        params={"id": category.pk},
        data={"features": {feature.slug: {"type": "string", "value": "a value"}}},
    )


# ── categories: feature editor ───────────────────────────────────────────────


@recipe("GET", "/categories/{id}/feature-editor/")
def _feature_editor_state(call):
    category, _feature = category_with_feature()
    # A root feature nobody uses, so `available_root_features` is not empty
    # either — both declared arrays carry a row.
    make_feature(name="Spare root feature")
    return call(staff_client(), params={"id": category.pk})


@recipe("POST", "/categories/{id}/feature-editor/draft/")
def _feature_editor_draft(call):
    category = make_category()
    return call(
        staff_client(),
        params={"id": category.pk},
        data={"draft": json.dumps({"features": []})},
    )


@recipe("POST", "/categories/{id}/feature-editor/apply/")
def _feature_editor_apply(call):
    """A real apply, not an empty batch — ``[]`` returns before any work."""
    category, feature = category_with_feature()
    make_feature(name="Spare root feature")
    category.refresh_from_db()
    return call(
        staff_client(),
        params={"id": category.pk},
        data={
            "base_revision": category.revision,
            "features": [
                {
                    "order": 0,
                    "action": "keep",
                    "feature": {
                        "id": feature.pk,
                        "name": feature.name,
                        "slug": feature.slug,
                        "config": feature.config,
                    },
                }
            ],
        },
    )


# ── categories: links ────────────────────────────────────────────────────────


@recipe("GET", "/categories/{id}/links/")
def _category_links_list(call):
    from stapel_categories.models import CategoryLink

    source = make_category(name="Wire Source")
    CategoryLink.objects.create(
        source=source, target=make_category(name="Wire Target"), order=0
    )
    return call(staff_client(), params={"id": source.pk})


@recipe("POST", "/categories/{id}/links/")
def _category_links_create(call):
    source = make_category(name="Wire Source")
    target = make_category(name="Wire Target")
    return call(
        staff_client(),
        params={"id": source.pk},
        data={"target": target.pk, "order": 0, "external_source": "storefront"},
    )


# ── categories: operator writes ──────────────────────────────────────────────


@recipe("POST", "/categories/{id}/undelete/")
def _categories_undelete(call):
    parent, child = parent_with_child()
    for row in (parent, child):
        row.deleted = True
        row.save()
    return call(staff_client(), params={"id": parent.pk})


@recipe("POST", "/categories/bulk_add/")
def _categories_bulk_add(call):
    category = make_category()
    return call(
        staff_client(),
        data=[{"id": category.pk, "name": "Renamed in bulk", "slug": category.slug}],
    )


@recipe("POST", "/categories/bulk-commands/")
def _categories_bulk_commands(call):
    return call(
        staff_client(),
        data={
            "categories": [
                {
                    "command": "add",
                    "name": "Added by command",
                    "slug": _unique("wire-cmd-"),
                }
            ]
        },
    )


@recipe("GET", "/categories/translation-keys/")
def _categories_translation_keys(call):
    """``IsServiceRequest``: a fleet service, not a person."""
    category, _feature = category_with_feature()
    assert category.translatable
    return call(anonymous(), HTTP_X_API_KEY=SERVICE_KEY)


# ── features ─────────────────────────────────────────────────────────────────


@recipe("GET", "/features/")
def _features_list(call):
    make_feature()
    return call(anonymous())


@recipe("POST", "/features/")
def _features_create(call):
    return call(
        staff_client(),
        data={
            "name": "Created by the wire gate",
            "slug": _unique("wire-new-feat-"),
            "config": dict(STRING_CONFIG),
        },
    )


@recipe("GET", "/features/{id}/")
def _features_retrieve(call):
    return call(anonymous(), params={"id": make_feature().pk})


@recipe("PUT", "/features/{id}/")
def _features_update(call):
    feature = make_feature()
    return call(
        staff_client(),
        params={"id": feature.pk},
        data={
            "name": "Replaced by the wire gate",
            "slug": feature.slug,
            "config": dict(STRING_CONFIG),
        },
    )


@recipe("PATCH", "/features/{id}/")
def _features_partial_update(call):
    return call(
        staff_client(),
        params={"id": make_feature().pk},
        data={"name": "Patched by the wire gate"},
    )


@recipe("POST", "/features/{id}/convert-type/")
def _features_convert_type(call):
    """select → string is one of the two conversions the view accepts."""
    feature = make_feature(config=dict(SELECT_CONFIG))
    return call(
        staff_client(),
        params={"id": feature.pk},
        data={"config": dict(STRING_CONFIG), "propagate": False},
    )


@recipe("POST", "/features/bulk_add/")
def _features_bulk_add(call):
    feature = make_feature()
    return call(
        staff_client(),
        data=[
            {
                "id": feature.pk,
                "name": "Renamed in bulk",
                "slug": feature.slug,
                "config": dict(STRING_CONFIG),
            }
        ],
    )


@recipe("GET", "/features/revision/")
def _features_revision(call):
    make_feature()
    return call(anonymous())


@recipe("GET", "/features/data.json/")
def _features_data_json(call):
    make_feature()
    return call(anonymous(), query="?revision=1")


# ─────────────────────────────────────────────────────────────────────────────
# The gate
# ─────────────────────────────────────────────────────────────────────────────


#: Operations whose declared body the wire does not send, with the defect and
#: its owner. ``strict=True``: a fixed entry fails until it is deleted, so a
#: finding can be neither forgotten nor quietly kept.
KNOWN_MISMATCHES: dict[tuple[str, str], str] = {}


def test_the_contract_declares_something_to_check():
    assert OPERATIONS, "docs/schema.json declares no JSON responses at all"


def test_every_declared_path_resolves_under_this_urlconf():
    """The suite must be looking where the document describes.

    Three of the first four libraries this gate was written for had a
    committed contract that nothing had ever driven, because the test urlconf
    mounted somewhere the document does not describe: one mounted a different
    prefix AND one segment short, one mounted the paths bare, and this one
    mounted less than the emission did — so the gdpr half of its own contract
    was unreachable. In every case the operations were "covered" by a file
    that could not have reached a single one of them.

    That is the same family as a gate nobody asks: the recipes can all be
    written, the run can be green, and not one request went where the contract
    says it goes. A missing recipe already fails loudly; this fails when the
    MOUNT is wrong, which no per-operation check can see, because when the
    mount is wrong every operation is equally and silently unreachable.

    Asserted against the urlconf this module declares, so it fails at the one
    moment it is cheap to fix: when somebody changes a mount.
    """
    from django.urls import Resolver404, resolve

    # Resolution cares about the SHAPE of a segment, and this urlconf uses
    # several converters — uuid, int, slug. A path counts as reachable if any
    # one shape resolves: the question here is whether the mount exists, not
    # whether a particular id does.
    candidates = (
        "00000000-0000-4000-8000-000000000000",
        "1",
        "a-slug",
    )

    unreachable = []
    for _method, path, _code, _schema in OPERATIONS:
        for value in candidates:
            try:
                resolve(re.sub(r"\{[^}]+\}", value, path))
                break
            except Resolver404:
                continue
        else:
            unreachable.append(path)

    assert not unreachable, (
        "these declared paths do not resolve under this module's urlconf, so "
        "nothing here can be driving them — the mount is wrong, not the "
        "recipes:\n  " + "\n  ".join(sorted(set(unreachable)))
    )


def test_every_declared_operation_is_driven_or_named_undrivable():
    """No operation is covered by silence, and no entry outlives its operation."""
    declared = {(method, path) for method, path, _code, _schema in OPERATIONS}
    covered = set(RECIPES) | set(UNDRIVABLE)

    missing = sorted(declared - covered)
    assert not missing, (
        "operations with a declared JSON response body and no recipe:\n"
        + "\n".join(f"  {m} {p}" for m, p in missing)
    )
    stale = sorted(covered - declared)
    assert not stale, (
        "recipes/exclusions for operations the contract no longer declares:\n"
        + "\n".join(f"  {m} {p}" for m, p in stale)
    )
    both = sorted(set(RECIPES) & set(UNDRIVABLE))
    assert not both, f"driven AND excluded: {both}"
    for key, reason in UNDRIVABLE.items():
        assert reason and reason.strip(), f"{key} is excluded with no reason"


def test_every_known_mismatch_is_still_declared_and_explained():
    """A recorded defect must name a live operation and carry its reason.

    Without this, an operation that is renamed or removed leaves an entry that
    silences nothing and reads like a known problem forever.
    """
    declared = {(method, path) for method, path, _code, _schema in OPERATIONS}
    for key, reason in KNOWN_MISMATCHES.items():
        assert key in declared, (
            f"{key} is recorded as a known mismatch but the contract no longer "
            "declares it — delete the entry"
        )
        assert reason and reason.strip(), f"{key} is recorded with no reason"


@pytest.mark.parametrize(
    "method,path,code,body_schema",
    OPERATIONS,
    ids=[f"{m} {p}" for m, p, _, _ in OPERATIONS],
)
def test_the_wire_matches_the_declared_response(method, path, code, body_schema, request):
    if (method, path) in UNDRIVABLE:
        pytest.skip(f"excluded by name: {UNDRIVABLE[(method, path)]}")

    if (method, path) in KNOWN_MISMATCHES:
        request.node.add_marker(
            pytest.mark.xfail(
                strict=True,
                reason=f"{method} {path}: {KNOWN_MISMATCHES[(method, path)]}",
            )
        )

    perform = RECIPES.get((method, path))
    assert perform is not None, (
        f"{method} {path} declares a response body and has no recipe — an "
        "unchecked operation is a schema nobody proves. Teach RECIPES, or "
        "name it in UNDRIVABLE with a reason."
    )

    response = perform(Call(method, path))
    assert response.status_code == code, (
        f"{method} {path}: expected the declared {code}, got "
        f"{response.status_code}: {response.content[:400]}"
    )

    body = response.json()
    errors = sorted(_validator(body_schema).iter_errors(body), key=lambda e: list(e.path))
    assert not errors, (
        f"{method} {path} answers a body the contract does not describe:\n"
        + "\n".join(f"  at {list(e.path) or '<root>'}: {e.message}" for e in errors[:10])
        + f"\n  body: {json.dumps(body)[:600]}"
    )
    # An empty collection validates against any item schema, so a collection
    # response must actually carry a row for the check to have looked at
    # anything — including the `results` page of the revision feed.
    if isinstance(body, list):
        assert body, f"{method} {path}: the declared list came back empty"
    if isinstance(body, dict) and isinstance(body.get("results"), list):
        assert body["results"], f"{method} {path}: the declared page came back empty"
