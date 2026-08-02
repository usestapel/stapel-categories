"""Drift gate for ``docs/llms.txt``, the fifth contract artifact
(stapel_tools.llms_txt).

``docs/capabilities.json`` in this module is HAND-WRITTEN (authored in the
stapel-catalog sweep, commit 1c69898) — there is no gate registry and no
codegen step to regenerate it from, so this gate does NOT cover
capabilities.json itself. It covers only ``docs/llms.txt``, which IS
generated (from capabilities.json) and therefore CAN drift the moment the
hand-written source changes underneath it without a `make contract` re-run —
exactly the silent-rot failure mode the fifth artifact exists to catch.
"""
from pathlib import Path

import pytest

pytest.importorskip(
    "stapel_tools",
    reason="stapel-tools carries the llms.txt emitter this gate runs.",
)

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
