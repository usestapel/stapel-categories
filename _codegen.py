"""stapel-categories contract-emission harness (contract-pipeline.md §2-3).

Emits the module's own contract triad into ``docs/`` from a single-module
``{categories + core}`` Django instance mounted at the canonical
``/categories/api/v1`` prefix (see ``_codegen_settings.py`` /
``codegen_urls.py``):

  docs/schema.json   drf-spectacular OpenAPI, this module only, canonical prefix
  docs/flows.json    generate_flow_docs machine artifact
  docs/errors.json   generate_error_keys registry (the per-module etalon)

stapel-categories is mounted in stapel-example-monolith, but there is no
aggregate slice diffed against this artifact for byte-identity — validation
is standalone (determinism + closure + canonical prefix), see
``tests/test_contract.py``.

Usage:
    python -m stapel_categories._codegen --out docs        # `make contract`
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _configure() -> None:
    """Configure + boot the single-module Django instance for emission."""
    # `python -m` prepends cwd to sys.path; strip the repo root the way a
    # flat-layout conftest does, so `import categories`-shaped collisions
    # cannot shadow anything.
    repo_root = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != repo_root]

    from django.conf import settings

    if not settings.configured:
        from stapel_categories._codegen_settings import settings_kwargs

        settings.configure(
            **settings_kwargs(root_urlconf="stapel_categories.codegen_urls", contract=True)
        )

    import django

    django.setup()

    # drf-spectacular froze its settings singleton at import time (before
    # configure() ran), so it is on drf defaults. The one knob to force is
    # SCHEMA_PATH_PREFIX: left None, drf derives the operationId prefix from
    # the common path of all endpoints — "/" across a multi-module aggregate
    # but "/categories/api" in a single-module harness, which would strip it
    # to bare anonymous names. Pin it to the aggregate convention.
    from drf_spectacular.settings import spectacular_settings

    from stapel_categories._codegen_settings import CODEGEN_SCHEMA_PATH_PREFIX

    spectacular_settings.SCHEMA_PATH_PREFIX = CODEGEN_SCHEMA_PATH_PREFIX

    # A real all-modules deployment registers drf-spectacular's JWT-cookie
    # security-scheme extension as a side effect of its dev-only Swagger
    # URLs. A single-module harness has no co-mounted sibling to trigger it,
    # so without this the protected endpoints would emit without their
    # `security` entry.
    from stapel_core.django.openapi.swagger import _register_jwt_auth_extension

    _register_jwt_auth_extension()


def _require_python_312() -> None:
    """Abort emission if not running the pinned 3.12 interpreter.

    drf-spectacular renders component descriptions (``Optional[X]`` vs
    ``X | None``) differently across Python minor versions, so emitting on
    anything but the CI/monolith pin produces false diffs against the
    committed docs/*.json.
    """
    if sys.version_info[:2] != (3, 12):
        got = f"{sys.version_info.major}.{sys.version_info.minor}"
        raise SystemExit(
            f"stapel-categories contract emission ABORTED: running Python {got}, "
            "but contracts must be emitted on Python 3.12 (the CI/monolith pin). "
            "Re-run under a 3.12 interpreter."
        )


def main(argv: list[str] | None = None) -> int:
    _require_python_312()

    parser = argparse.ArgumentParser(
        prog="stapel-categories-contract",
        description="Emit this module's contract triad (schema.json + flows.json "
        "+ errors.json) into --out, canonical /categories/api/v1 prefix.",
    )
    parser.add_argument("--out", default="docs", help="Output directory (default: docs).")
    args = parser.parse_args(argv)

    _configure()

    from stapel_tools.codegen import emit_errors, emit_flows, emit_schema

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    paths = emit_schema(out / "schema.json")
    flows = emit_flows(out / "flows.json")
    errors = emit_errors(out / "errors.json")

    print(
        f"stapel-categories contract: {paths} paths, {flows} flows, {errors} error keys "
        f"-> {out}/",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
