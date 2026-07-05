"""comm surface of stapel-categories.

Every Function/Action carries a JSON schema in ``schemas/`` — tests run
with ``VALIDATE_SCHEMAS`` on, so a payload drifting from its schema fails
loudly. Registration happens on import from ``apps.py:ready()``; re-imports
are no-ops. Other modules call by name, no import of this package needed:

    from stapel_core.comm import call

    call("categories.features", {"category_id": 42})
    # -> {"category_id": 42, "revision": 7, "features": [ {slug, config, ...} ]}

``categories.features`` returns the *resolved* feature definitions for a
category (own + inherited, config merged with type defaults). stapel-listings
calls it to validate listing attribute values against the category schema
WITHOUT importing this module; the payload is cacheable by ``revision``.
Mutations emit ``category.changed`` (see events.py) for cache invalidation.
"""
import json
from pathlib import Path

from stapel_core.comm import function

_SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas" / "functions"

# Bounded retries for the consistent (revision, features) snapshot read below.
_FEATURES_SNAPSHOT_RETRIES = 3


def _schema(name: str) -> dict:
    """Load a committed contract — one source of truth, no inline copy."""
    return json.loads((_SCHEMAS_DIR / f"{name}.json").read_text(encoding="utf-8"))


@function("categories.features", schema=_schema("categories.features"))
def features_function(payload: dict) -> dict:
    """Resolve the feature schema for a category.

    Payload: ``{"category_id": <int>}``. Returns
    ``{"category_id": int, "revision": int, "features": [FeatureDef]}`` where
    each FeatureDef is ``{id, slug, name, mandatory, config}`` — ``config``
    is merged with its type's defaults. Raises ``LookupError`` (missing
    category) so callers can distinguish "no such category" from "no
    features".
    """
    from .models import Category

    category_id = payload["category_id"]
    try:
        category = Category.objects.get(pk=category_id)
    except Category.DoesNotExist:
        raise LookupError(f"category {category_id} not found") from None

    # M-6: revision and features must come from ONE snapshot. Under READ
    # COMMITTED the row read (revision) and feature_defs() (its own SELECTs)
    # are separate statements, so a concurrent apply committing between them
    # yields a torn pair — e.g. an old revision with new features, which a
    # consumer would then cache forever under the stale revision. Read the
    # revision on both sides of feature_defs() and retry until it is stable,
    # so the returned (revision, features) pair is internally consistent.
    for _ in range(_FEATURES_SNAPSHOT_RETRIES):
        revision_before = (
            Category.objects.values_list("revision", flat=True).get(pk=category.pk)
        )
        features = category.feature_defs()
        revision_after = (
            Category.objects.values_list("revision", flat=True).get(pk=category.pk)
        )
        if revision_before == revision_after:
            break
        category.refresh_from_db()
    else:
        # Never converged (constant churn) — return the last consistent read of
        # the revision paired with those features; refresh once more so at least
        # revision_after describes the same read.
        revision_after = (
            Category.objects.values_list("revision", flat=True).get(pk=category.pk)
        )

    return {
        "category_id": category.pk,
        "revision": revision_after,
        "features": features,
    }
