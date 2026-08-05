"""Drift gate for ``docs/llms.txt``, the fifth contract artifact
(stapel_tools.llms_txt).

``docs/capabilities.json`` in this module is otherwise HAND-WRITTEN (authored
in the stapel-catalog sweep, commit 1c69898) — there is no gate registry and
no codegen step to derive axes from, so this gate does NOT cover
capabilities.json itself (that is tests/test_capabilities_surface.py's job,
for the derived ``surface`` section). It covers ``docs/llms.txt``, which IS
generated (from capabilities.json) and therefore CAN drift the moment the
hand-written source OR the derived surface changes underneath it without a
`make contract` re-run — exactly the silent-rot failure mode the fifth
artifact exists to catch.
"""
from pathlib import Path

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
COMMITTED = REPO / "docs" / "llms.txt"


def test_llms_txt_committed():
    assert COMMITTED.is_file(), "missing docs/llms.txt — run `make contract`"


def test_llms_txt_has_no_drift():
    rendered = render(load_inputs(REPO))
    assert COMMITTED.read_text() == rendered, (
        "docs/llms.txt is stale — run `make contract` and commit it"
    )


def test_llms_txt_emission_is_deterministic():
    """Two independent renders are byte-identical (the drift gate is meaningful)."""
    a = render(load_inputs(REPO))
    b = render(load_inputs(REPO))
    assert a == b
