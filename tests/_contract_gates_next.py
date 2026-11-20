# Verbatim copy of stapel_tools.contract_gates (stapel-tools main + the auth-state replay),
# until a stapel-tools release carries AuthStateReplay; then import from stapel_tools.
"""Two contract gates every brick's test suite calls on itself.

``invalid_examples(schema)``
    Every example in an emitted OpenAPI document (``docs/schema.json``,
    ``docs/schema.v2.json``) validates against the schema it illustrates. A
    generated client and a generated test both read examples as facts: a
    native client's suite found 16 of 72 invalid in one brick (a partial
    object, ``"True"`` for a boolean, a required field missing because the
    docstring reader dropped a falsy example). Needs ``jsonschema`` (a
    dependency of drf-spectacular, so present wherever a schema is emitted).

``dead_action_declarations(resolver=None)``
    An ``@action(permission_classes=...)`` is applied only when a router
    builds the route; a viewset mounted with ``as_view({...})`` never reads
    the action's kwargs, so the declaration is text that reads like a gate.
    Walks every mounted viewset route and returns the declarations whose
    classes the mount does not run (a stricter mount is not dead).

``response_errors(schema, method, path, status, body)``
    A REAL response checked against what the document declares for that
    operation and status. Examples are claims written next to the schema; a
    native client decodes what the server sends, and a brick served bodies
    its own schema forbids (integer tiers, required keys missing for a caller
    without a session) while every example was valid.

``AuthStateReplay``
    Every request a wire recipe makes to the operation under test is sent
    again as each caller state the brick names (no session, a guest session,
    a registered stranger), and each answer is checked: a 2xx is a declared
    status with a body that validates, a refusal is a 4xx, never a 5xx, and
    an operation the document opens to callers without a session does not
    answer them 401. The recipe's own caller (usually the owner) is the
    fourth state, checked by the recipe's gate as before.

Usage in a brick (``tests/test_contract_examples.py``)::

    from stapel_tools.contract_gates import invalid_examples
    bad, checked = invalid_examples(json.loads(path.read_text()))
    assert checked >= N and bad == {}
"""
from __future__ import annotations

import copy
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

__all__ = [
    "AuthStateReplay",
    "WireFinding",
    "dead_action_declarations",
    "examples",
    "invalid_examples",
    "operation_template",
    "response_errors",
    "viewset_routes",
]

#: The property flag a value in the given direction must not carry.
_FORBIDDEN_IN = {"request": "readOnly", "response": "writeOnly"}


def _blank_string_alternative(node: dict) -> bool:
    """``URLField(allow_blank=True)`` emits ``oneOf: [{format: uri}, {maxLength: 0}]``.

    ``format`` is an annotation in JSON Schema, so ``""`` matches both
    branches and the exclusive ``oneOf`` refuses a value the document allows.
    """
    branches = node.get("oneOf")
    return isinstance(branches, list) and any(
        isinstance(b, dict) and b.get("type") == "string" and b.get("maxLength") == 0
        for b in branches
    )


def _undiscriminated_union(node: dict) -> bool:
    """A ``oneOf`` of bare ``$ref`` branches with no ``discriminator``.

    ``PolymorphicProxySerializer(resource_type_field_name=None)`` emits
    branches that may overlap by construction (one is another plus a key), so
    a value satisfying both is one the document describes, and an exclusive
    ``oneOf`` would refuse it. A discriminated union stays exclusive.
    """
    branches = node.get("oneOf")
    if not isinstance(branches, list) or "discriminator" in node:
        return False
    return all(isinstance(b, dict) and set(b) == {"$ref"} for b in branches)


def _openapi30_to_jsonschema(node: Any, direction: str | None = None) -> Any:
    """OpenAPI 3.0 to JSON Schema, optionally for one direction of travel.

    ``nullable: true`` becomes a null branch. With ``direction`` set, a
    property flagged ``readOnly`` is forbidden in a ``"request"`` value and a
    ``writeOnly`` one in a ``"response"`` value, and neither is required
    there: one component describes both directions, and only the flags say
    which half a value may carry.
    """
    if isinstance(node, list):
        return [_openapi30_to_jsonschema(n, direction) for n in node]
    if not isinstance(node, dict):
        return node
    out = {k: _openapi30_to_jsonschema(v, direction) for k, v in node.items() if k != "nullable"}
    flag = _FORBIDDEN_IN.get(direction or "")
    if flag and isinstance(node.get("properties"), dict):
        banned = {name for name, prop in node["properties"].items() if isinstance(prop, dict) and prop.get(flag)}
        if banned:
            # `not: {}` rather than `false`: jsonschema reports a `false`
            # property schema at the object, without the property's name.
            out["properties"] = {
                name: ({"not": {}} if name in banned else prop) for name, prop in out["properties"].items()
            }
            if "required" in out:
                out["required"] = [name for name in out["required"] if name not in banned]
    if _blank_string_alternative(out) or _undiscriminated_union(out):
        out["anyOf"] = out.pop("oneOf")
    if node.get("nullable"):
        return {"anyOf": [out, {"type": "null"}]}
    return out


def examples(schema: dict) -> Iterator[tuple[tuple[str, str, str, str], dict, Any]]:
    """``((method, path, where, name), media schema, value)`` for every example.

    *where* is ``"request"`` or the response status; *name* is the example's
    key, or ``"example"`` for a media-level single example.
    """
    for path, item in (schema.get("paths") or {}).items():
        for method, op in item.items():
            if not isinstance(op, dict):
                continue
            body = (op.get("requestBody") or {}).get("content") or {}
            medias = [("request", m) for m in body.values()]
            for status, response in (op.get("responses") or {}).items():
                medias += [(status, m) for m in (response.get("content") or {}).values()]
            for where, media in medias:
                if "schema" not in media:
                    continue
                if "example" in media:
                    yield (method, path, where, "example"), media["schema"], media["example"]
                for name, ex in (media.get("examples") or {}).items():
                    if isinstance(ex, dict) and "value" in ex:
                        yield (method, path, where, name), media["schema"], ex["value"]


def invalid_examples(schema: dict) -> tuple[dict[tuple[str, str, str, str], list[str]], int]:
    """``({(method, path, where, name): [error, ...]}, number of examples checked)``.

    Request examples are validated against the request direction of their
    schema: a ``readOnly`` field in a request example is an error (a client
    copying the example sends a field the server ignores or refuses), and a
    ``writeOnly`` one in a response example likewise.

    The count is returned so a caller can assert the walk saw its subject:
    an empty result over zero examples proves nothing.
    """
    from jsonschema import Draft202012Validator

    raw = schema.get("components") or {}
    components = {d: _openapi30_to_jsonschema(copy.deepcopy(raw), d) for d in _FORBIDDEN_IN}
    bad: dict[tuple[str, str, str, str], list[str]] = {}
    checked = 0
    for key, media_schema, value in examples(schema):
        checked += 1
        # A request example is checked against the write half of its schema
        # (no readOnly field), a response example against the read half.
        direction = "request" if key[2] == "request" else "response"
        root = {**_openapi30_to_jsonschema(media_schema, direction), "components": components[direction]}
        errors = [
            f"{list(e.absolute_path)}: {_message(e, direction)[:160]}"
            for e in Draft202012Validator(root).iter_errors(value)
        ]
        if errors:
            bad[key] = errors
    return bad, checked


_ACTION_ONLY_KWARGS = {
    "permission_classes": "get_permissions",
    "authentication_classes": "get_authenticators",
    "throttle_classes": "get_throttles",
}


def _viewset_routes(resolver, prefix: str = ""):
    for pattern in resolver.url_patterns:
        if hasattr(pattern, "url_patterns"):
            yield from _viewset_routes(pattern, prefix + str(pattern.pattern))
            continue
        callback = pattern.callback
        actions = getattr(callback, "actions", None)
        cls = getattr(callback, "cls", None)
        if actions and cls is not None:
            yield prefix + str(pattern.pattern), callback, cls, actions


def viewset_routes(resolver=None):
    """``(route, callback, view class, {method: action})`` per mounted viewset route."""
    if resolver is None:
        from django.urls import get_resolver

        resolver = get_resolver()
    return list(_viewset_routes(resolver))


def _effective_classes(callback, cls, name: str, kwarg: str) -> set:
    view = cls(**(getattr(callback, "initkwargs", {}) or {}))
    view.action = name
    view.action_map = getattr(callback, "actions", {})
    return {type(obj) for obj in getattr(view, _ACTION_ONLY_KWARGS[kwarg])()}


def dead_action_declarations(resolver=None) -> list[tuple[str, str, str]]:
    """``[(route, "Class.action", kwarg)]`` whose declared classes do not all run."""
    dead = []
    for route, callback, cls, actions in viewset_routes(resolver):
        for name in set(actions.values()):
            declared = getattr(getattr(cls, name, None), "kwargs", {}) or {}
            for kwarg in _ACTION_ONLY_KWARGS:
                if kwarg not in declared:
                    continue
                if not set(declared[kwarg]) <= _effective_classes(callback, cls, name, kwarg):
                    dead.append((route, f"{cls.__name__}.{name}", kwarg))
    return sorted(set(dead))


# ─────────────────────────────────────────────────────────────────────────────
# Real responses against the document
# ─────────────────────────────────────────────────────────────────────────────


def _message(error, direction: str) -> str:
    if error.validator == "not" and error.validator_value == {}:
        return f"a {_FORBIDDEN_IN[direction]} field has no place in a {direction}"
    if error.context:
        # "<the whole value> is not valid under any of the given schemas"
        # names nothing; the closest branch's own error does.
        from jsonschema.exceptions import best_match

        inner = best_match(error.context)
        return f"{list(inner.absolute_path) or '<root>'}: {_message(inner, direction)}"
    return error.message


def _template_regex(template: str) -> re.Pattern:
    parts = re.split(r"(\{[^}/]+\})", template)
    return re.compile(
        "^" + "".join("[^/]+" if p.startswith("{") else re.escape(p) for p in parts) + "$"
    )


def operation_template(schema: dict, method: str, url_path: str) -> str | None:
    """The declared path template a concrete request path answers to, or None.

    A literal segment beats a parameter (``/me`` before ``/{user_id}``), the
    way a router resolves them.
    """
    method = method.lower()
    matches = [
        template
        for template, item in (schema.get("paths") or {}).items()
        if method in item and _template_regex(template).match(url_path)
    ]
    if not matches:
        return None
    return min(matches, key=lambda t: (t.count("{"), -len(t)))


def response_errors(schema: dict, method: str, path: str, status: int, body: Any) -> list[str]:
    """Errors of a real JSON ``body`` against the declared response.

    ``path`` is the template as the document spells it. A status the
    operation does not declare is an error by itself; a declared status
    with no JSON content checks nothing more (an empty body is the claim).
    """
    from jsonschema import Draft202012Validator

    op = (schema.get("paths") or {}).get(path, {}).get(method.lower())
    if op is None:
        return [f"{method.upper()} {path} is not declared"]
    response = (op.get("responses") or {}).get(str(status))
    if response is None:
        return [f"status {status} is not declared (declared: {', '.join(sorted(op.get('responses') or {}))})"]
    media = (response.get("content") or {}).get("application/json")
    if media is None or "schema" not in media:
        return []
    components = _openapi30_to_jsonschema(copy.deepcopy(schema.get("components") or {}), "response")
    root = {**_openapi30_to_jsonschema(media["schema"], "response"), "components": components}
    return [
        f"at {list(e.absolute_path) or '<root>'}: {_message(e, 'response')[:200]}"
        for e in sorted(Draft202012Validator(root).iter_errors(body), key=lambda e: list(e.absolute_path))
    ]


def _open_to_no_session(op: dict) -> bool:
    """The operation's ``security`` lists the empty requirement (no session)."""
    security = op.get("security")
    return security is None or any(not req for req in security)


@dataclass
class WireFinding:
    """One answer, for one caller state, that the document does not describe."""

    state: str
    method: str
    path: str
    status: int
    problem: str

    def __str__(self) -> str:
        return f"{self.method} {self.path} as {self.state}: {self.status} — {self.problem}"


#: Request headers that carry a caller's credential: a session, or the fleet
#: service key (``X-API-KEY``, core's ServiceAPIKeyMiddleware).
_AUTH_HEADERS = ("HTTP_AUTHORIZATION", "HTTP_COOKIE", "HTTP_X_API_KEY")


@dataclass
class AuthStateReplay:
    """Replays a recipe's requests to one operation as other caller states.

    ``schemas`` maps a name to a document (``{"schema.v2.json": ..., "schema.json": ...}``);
    every answer is checked against each, and the FIRST document's
    ``security`` is the claim about who may call (profile 2 derives it from
    the running permission stack; profile 1 does not). ``states`` maps a state name to a
    zero-argument factory returning a fresh client already authenticated as
    that state (``{"anonymous": APIClient, "guest": ..., "member": ...}``);
    the state named ``"anonymous"`` is the caller with no session at all.

    Use::

        replay = AuthStateReplay(schemas, states)
        with replay.watching("GET", "/profiles/api/v1/{user_id}"):
            recipe(...)            # its own calls are answered as before
        assert replay.replayed, "the recipe never called the operation"
        assert not replay.findings

    Each matching request is replayed right after the original returns, so
    the replay sees the recipe's settings overrides and database state at
    that moment. Request headers that carry a credential are dropped.
    Refusals with a status the operation does not declare are collected in
    ``undeclared_refusals``, not ``findings``: what a client decodes is the
    2xx body; an undeclared 4xx reaches it as an error either way.
    """

    schemas: dict
    states: dict
    findings: list = field(default_factory=list)
    undeclared_refusals: list = field(default_factory=list)
    replayed: int = 0

    def _primary(self) -> dict:
        return next(iter(self.schemas.values()))

    def check(self, state: str, method: str, path: str, response) -> None:
        """Judge one answer for ``state`` (also usable on the recipe's own answers)."""
        status = response.status_code
        op = self._primary()["paths"][path][method.lower()]
        declared = op.get("responses") or {}

        def found(problem):
            self.findings.append(WireFinding(state, method.upper(), path, status, problem))

        if status >= 500:
            found(f"server error: {response.content[:200]!r}")
            return
        if 200 <= status < 300:
            if state == "anonymous" and not _open_to_no_session(op):
                found("served without a session, but the operation's security requires one")
            if str(status) not in declared:
                found(f"2xx status not declared (declared: {', '.join(sorted(declared))})")
                return
            body = _json_or_none(response)
            for name, schema in self.schemas.items():
                for error in response_errors(schema, method, path, status, body):
                    found(f"{name}: {error}")
            return
        if status == 401 and state == "anonymous" and _open_to_no_session(op):
            found("the operation is declared open to callers without a session, and refuses them 401")
            return
        if str(status) not in declared:
            self.undeclared_refusals.append(WireFinding(state, method.upper(), path, status, "undeclared refusal"))
            return
        body = _json_or_none(response)
        for name, schema in self.schemas.items():
            for error in response_errors(schema, method, path, status, body):
                found(f"{name}: refusal body: {error}")

    @contextmanager
    def watching(self, method: str, path: str):
        from rest_framework.test import APIClient

        original = APIClient.generic
        replay = self
        busy = []

        def generic(client, verb, url, data="", content_type="application/octet-stream", secure=False, **extra):
            response = original(client, verb, url, data, content_type, secure, **extra)
            bare = url.split("?", 1)[0]
            if (
                busy
                or verb.upper() != method.upper()
                or operation_template(replay._primary(), verb, bare) != path
            ):
                return response
            clean = {k: v for k, v in extra.items() if k not in _AUTH_HEADERS}
            busy.append(True)
            try:
                for state, make_client in replay.states.items():
                    other = make_client()
                    answer = original(other, verb, url, data, content_type, secure, **clean)
                    replay.replayed += 1
                    replay.check(state, method, path, answer)
            finally:
                busy.pop()
            return response

        APIClient.generic = generic
        try:
            yield self
        finally:
            APIClient.generic = original


def _json_or_none(response) -> Any:
    import json

    content = getattr(response, "content", b"")
    if not content:
        return None
    try:
        return json.loads(content)
    except ValueError:
        return content.decode("utf-8", "replace")
