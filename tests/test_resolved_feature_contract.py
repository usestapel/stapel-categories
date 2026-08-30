"""``$defs.ResolvedFeature`` is gated against the FeatureDef canon (spec §2.2).

``stapel-attributes/docs/feature-def.schema.json`` is the single source of
truth for the shape of a feature definition (§68: one JSON, a fan of
emitters). ``categories.features`` is one of those emitters — it is what
stapel-listings feeds to ``coerce_feature_defs`` — so a field added to the
canon and not to this response is a field that silently stops crossing the
boundary. The whole class of bug the check exists for is the quiet one:
``rules`` dropped in transit reverts a category to static ``mandatory``, and
nothing fails, it just answers wrong.

``config`` is the one exemption: the canon describes it as an opaque
``{type, ...}`` object with the per-type shapes living with their plugins,
and this schema says the same thing in its own words.

The canon is read from the sibling checkout when the workspace has one (so a
developer editing both repos at once sees the drift immediately) and from the
INSTALLED package otherwise — the schema is package data, shipped in the
wheel, so CI resolves it too. There is deliberately no skip: a contract gate
that quietly disables itself when the upstream checkout is missing is the
gate-that-proves-nothing pattern.
"""
import json
from importlib.resources import files
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_MODULE_ROOT = _HERE.parent
_SIBLING_CANON = _MODULE_ROOT.parent / "stapel-attributes" / "docs" / "feature-def.schema.json"

RESOLVED_FEATURE_EXEMPT = {"config"}


def _installed_canon_path() -> Path:
    """The canon as shipped inside the stapel-attributes distribution."""
    return Path(str(files("stapel_attributes") / "docs" / "feature-def.schema.json"))


def _feature_def_properties(canon: dict) -> set:
    return set(canon["$defs"]["FeatureDef"]["properties"])


@pytest.fixture(scope="module")
def resolved_feature() -> dict:
    schema = json.loads(
        (_MODULE_ROOT / "schemas" / "functions" / "categories.features.json").read_text(
            encoding="utf-8"
        )
    )
    return schema["$defs"]["ResolvedFeature"]


@pytest.fixture(scope="module")
def canon() -> dict:
    """The canon, preferring a sibling checkout over the installed copy."""
    path = _SIBLING_CANON if _SIBLING_CANON.is_file() else _installed_canon_path()
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_installed_canon_is_always_readable():
    """The wheel ships it, so this check never degrades to a skip in CI."""
    path = _installed_canon_path()
    assert path.is_file(), (
        f"stapel-attributes ships no {path} — the FeatureDef canon must be "
        "package data (pyproject [tool.setuptools.package-data]) or every "
        "consumer's contract gate silently loses its reference"
    )
    canon = json.loads(path.read_text(encoding="utf-8"))
    assert _feature_def_properties(canon), "the installed canon declares no FeatureDef properties"


def test_resolved_feature_covers_every_canon_property(resolved_feature, canon):
    expected = _feature_def_properties(canon) - RESOLVED_FEATURE_EXEMPT
    described = set(resolved_feature["properties"])
    missing = expected - described
    assert not missing, (
        f"$defs.ResolvedFeature is missing canon propert(ies) {sorted(missing)} — "
        "stapel-attributes' FeatureDef grew a field that categories.features "
        "does not carry, so it never reaches a consumer"
    )


def test_every_canon_property_is_required(resolved_feature, canon):
    """A consumer builds a FeatureDef from this payload without a second call.

    Optional means "the producer may omit it", which for `rules` means
    "requiredness may silently fall back to `mandatory`" — so the response
    always sends all of them, blank/empty rather than absent.
    """
    expected = _feature_def_properties(canon) - RESOLVED_FEATURE_EXEMPT
    required = set(resolved_feature["required"])
    assert expected <= required, (
        f"canon propert(ies) {sorted(expected - required)} are described but "
        "not required by $defs.ResolvedFeature"
    )
    assert "config" in required


def test_the_sibling_and_installed_canons_agree():
    """When the workspace has both, they must be the same document.

    A stale editable install is otherwise indistinguishable from a passing
    gate: the test would read the checkout while every runtime import reads
    the older installed copy.
    """
    if not _SIBLING_CANON.is_file():
        pytest.skip("no sibling stapel-attributes checkout in this workspace")
    sibling = json.loads(_SIBLING_CANON.read_text(encoding="utf-8"))
    installed = json.loads(_installed_canon_path().read_text(encoding="utf-8"))
    assert _feature_def_properties(sibling) == _feature_def_properties(installed)


def test_the_exemption_is_the_documented_one(resolved_feature, canon):
    """``config`` is exempt because both sides describe it in their own words."""
    assert "config" in _feature_def_properties(canon)
    assert resolved_feature["properties"]["config"]["type"] == "object"
