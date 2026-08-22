"""The committed contract artifacts must describe the API that ships.

``make contract-check`` re-emits docs/{schema,flows,errors}.json via
``stapel_categories._codegen`` and diffs them, but it needs the pinned
Python 3.12 interpreter plus stapel-tools, so it is a dev-loop and release
gate rather than something a wider CI matrix can run unpinned. The tests
below are the part that runs everywhere: they read the COMMITTED artifacts
and assert the two properties a stale artifact silently breaks — every
route this module mounts is described, and every error key it can return is
declared (A1, darom-storefront-design.md §3.10 — the contract triad the
react codegen pipeline, ``gen:api``/``gen:errors``/``gen:manifest``, stands
on).

``docs/capabilities.json`` remains HAND-WRITTEN (authored in the
stapel-catalog sweep, commit 1c69898) — there is no gate registry and no
codegen step to derive axes from, so this gate does NOT cover
capabilities.json itself (that is tests/test_capabilities_surface.py's job,
for the derived ``surface`` section). ``docs/llms.txt`` and README.md ARE
fully generated and gated below, same as before.
"""
import json
import re
from pathlib import Path

import pytest

try:
    import stapel_tools  # noqa: F401  (probe: the emitter must be importable)
except ImportError as exc:  # pragma: no cover - environment failure, not a branch
    # NOT pytest.importorskip. A drift gate that skips when its emitter is
    # missing reports `1 skipped`, exits 0, and disappears among a hundred
    # green tests — making "the tool is absent" indistinguishable from "there
    # is no drift". A gate that cannot run has FAILED; it has not passed.
    raise RuntimeError(
        "llms.txt drift gate cannot run: stapel-tools is not importable, and "
        "it carries the emitter this gate measures drift against. Install it "
        "(workspace venv, or `pip install stapel-tools`) and re-run. This is "
        "a hard failure on purpose — a skipped drift gate is silently no "
        "gate."
    ) from exc

from stapel_tools.llms_txt import load_inputs, render  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
COMMITTED = DOCS / "llms.txt"

# stapel-categories' surface + the new errors/operations sections push the
# rendered file past the generator's default 4000-token budget — the same
# deliberate exception stapel-forms (5000), stapel-recordings (5000) and
# stapel-workspaces (4500) already take (see the Makefile `contract` target).
LLMS_TXT_BUDGET = 5000


@pytest.fixture(scope="module")
def errors_artifact():
    return {entry["code"]: entry for entry in json.loads((DOCS / "errors.json").read_text())}


@pytest.fixture(scope="module")
def schema_artifact():
    return json.loads((DOCS / "schema.json").read_text())


# ── errors.json ──────────────────────────────────────────────────────


def test_categories_owned_keys_are_declared(errors_artifact):
    from stapel_categories.errors import STAPEL_CATEGORIES_ERRORS

    for code, text in STAPEL_CATEGORIES_ERRORS.items():
        assert code in errors_artifact, f"{code} missing from docs/errors.json"
        assert errors_artifact[code]["owner"] == "stapel_categories"
        assert errors_artifact[code]["en"] == text


def test_the_attributes_validation_family_is_declared(errors_artifact):
    """The feature-editor / validate-dto paths reach stapel-attributes' codes too.

    Owned by (and translated by) stapel-attributes: this module only forces
    the registration (``import stapel_attributes.errors`` in errors.py) so
    the keys land wherever categories is mounted. Asserting the OWNER too is
    the half that keeps this honest — copying the strings into
    ``STAPEL_CATEGORIES_ERRORS`` would take on a catalogue obligation that is
    upstream's.
    """
    from stapel_attributes.errors import ATTRIBUTES_ERRORS

    assert ATTRIBUTES_ERRORS, "the attributes registry came back empty"
    for code in ATTRIBUTES_ERRORS:
        assert code in errors_artifact, f"{code} missing from docs/errors.json"
        assert errors_artifact[code]["owner"] == "stapel_attributes"


def test_the_error_keys_stay_categories_owned():
    """This module's own catalogue must not claim upstream's keys as its own."""
    from stapel_attributes.errors import ATTRIBUTES_ERRORS
    from stapel_categories.errors import STAPEL_CATEGORIES_ERRORS

    assert not set(STAPEL_CATEGORIES_ERRORS) & set(ATTRIBUTES_ERRORS)


# ── schema.json ──────────────────────────────────────────────────────


def test_every_mounted_route_is_described(schema_artifact):
    """A route added without regenerating the triad fails here.

    The gap this closes is not hypothetical: the pair reads ``schema.json``
    to generate its typed client, so an endpoint missing from the artifact is
    an endpoint the frontend cannot call.
    """
    from stapel_categories import urls_v1

    described = set(schema_artifact["paths"])
    seen_names = set()
    for pattern in urls_v1.urlpatterns:
        name = getattr(pattern, "name", None)
        # DefaultRouter also emits the format-suffix twin of every route
        # (`categories.json`) and the browsable api-root — neither is a real
        # product endpoint.
        if name is None or name in seen_names or name == "api-root":
            continue
        seen_names.add(name)
        route = str(pattern.pattern)
        if "format" in route:
            continue
        # `(?P<pk>[^/.]+)` -> `{id}` (the model's pk field is `id`);
        # any other named group -> `{name}` verbatim.
        route = re.sub(r"\(\?P<pk>[^)]+\)", "{id}", route)
        route = re.sub(r"\(\?P<([a-zA-Z_]+)>[^)]+\)", r"{\1}", route)
        route = route.strip("^$")
        assert f"/categories/api/v1/{route}" in described, (
            f"{route} is mounted but absent from docs/schema.json — "
            "run 'make contract' and commit the artifacts"
        )


def test_feature_config_discriminator_is_slug_keyed(schema_artifact):
    """Regression: discriminator.mapping used to collapse to one "null" key.

    stapel-attributes' PolymorphicProxySerializer used to be built from a
    bare list of serializer classes; drf-spectacular then *inferred* each
    resource_type by calling ``to_representation(None)`` on the sub-
    serializer's ``type`` field. DRF's ``ChoiceField.to_representation``
    short-circuits ``None``/``''`` straight back to ``None`` instead of
    resolving the field's constant value, so every one of the ten feature
    types inferred as ``None`` — the mapping dict comprehension collapsed to
    a single ``{None: <last schema>}``, serialized as one bogus ``"null"``
    key. openapi-typescript then stripped ``type`` from generated call
    sites and re-added a synthetic wrong one (fixed upstream in
    stapel-attributes 0.4.7, see its CHANGELOG).
    """
    from stapel_attributes.registry import get_all_type_slugs

    slugs = set(get_all_type_slugs())
    assert len(slugs) == 10

    for component_name in ("FeatureConfig", "FeatureDto"):
        component = schema_artifact["components"]["schemas"][component_name]
        discriminator = component["discriminator"]
        mapping = discriminator["mapping"]

        assert "null" not in mapping, (
            f"{component_name}.discriminator.mapping has a bogus \"null\" key — "
            "stapel-attributes floor needs bumping to >=0.4.7 and 'make contract' re-run"
        )
        assert set(mapping) == slugs, (
            f"{component_name}.discriminator.mapping keys {sorted(mapping)} "
            f"don't match the registered type slugs {sorted(slugs)}"
        )
        for slug, ref in mapping.items():
            assert ref.startswith("#/components/schemas/"), (
                f"{component_name}.discriminator.mapping[{slug!r}] = {ref!r} "
                "is not a component ref"
            )


# ── docs/llms.txt — the fifth contract artifact -----------------------------


def test_llms_txt_committed():
    assert COMMITTED.is_file(), "missing docs/llms.txt — run `make contract`"


def test_llms_txt_has_no_drift():
    rendered = render(load_inputs(REPO), budget=LLMS_TXT_BUDGET)
    assert COMMITTED.read_text() == rendered, (
        "docs/llms.txt is stale — run `make contract` and commit it"
    )


def test_llms_txt_emission_is_deterministic():
    """Two independent renders are byte-identical (the drift gate is meaningful)."""
    a = render(load_inputs(REPO), budget=LLMS_TXT_BUDGET)
    b = render(load_inputs(REPO), budget=LLMS_TXT_BUDGET)
    assert a == b


# --- README.md — the sixth artifact (tracker #257) ---------------------------
#
# README.md is assembled by ``stapel_tools.readme`` from docs/readme.md (the
# human half: what this module is and how to think about it) plus the contract
# documents above (badges, version, surface counts, doc links). Everything a
# hand-written README used to restate — and therefore used to get wrong one
# release later — is generated here and gated below.

def test_readme_is_assembled_and_has_no_drift():
    from stapel_tools.readme import load_inputs as load_readme_inputs, render as render_readme, static_languages

    inputs = load_readme_inputs(REPO)
    languages = static_languages(REPO)
    assert languages == ["en"], "expected exactly the English static body docs/readme.md"
    committed = (REPO / "README.md").read_text()
    assert committed == render_readme(REPO, inputs, "en", languages), (
        "README.md drifted — run `make contract` and commit README.md "
        "(edit prose in docs/readme.md, never README.md itself)"
    )


def test_readme_version_matches_the_package():
    """The #226 gate, at the point where the number is published."""
    import tomllib

    from stapel_tools.readme import load_inputs as load_readme_inputs, resolve_version

    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert resolve_version(load_readme_inputs(REPO)) == pyproject["project"]["version"]
