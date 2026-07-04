"""comm event publishers for stapel-categories.

Events go through ``stapel_core.comm`` (transport is deployment
configuration: in-process in a monolith, bus in microservices). Emitted
through the transactional outbox, so the event leaves iff the surrounding
DB transaction commits. Payload contract lives in
``schemas/emits/category.changed.json``.

Downstream consumers (e.g. stapel-listings) subscribe to ``category.changed``
to invalidate any cached ``categories.features`` result for that category.
"""
import logging

logger = logging.getLogger(__name__)


def publish_category_changed(category_id, revision):
    """Emit ``category.changed`` for a mutated category (or feature affecting it)."""
    try:
        from stapel_core.comm import emit

        emit(
            "category.changed",
            {"category_id": int(category_id), "revision": int(revision or 0)},
            key=str(category_id),
        )
    except Exception:
        logger.exception("Failed to publish category-changed event")
