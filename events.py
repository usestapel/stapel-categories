"""comm event publishers for stapel-categories.

Events go through ``stapel_core.comm`` (transport is deployment
configuration: in-process in a monolith, bus in microservices). Emitted
through the transactional outbox, so the event leaves iff the surrounding
DB transaction commits. Payload contract lives in
``schemas/emits/category.changed.json``.

Downstream consumers (e.g. stapel-listings) subscribe to ``category.changed``
to invalidate any cached ``categories.features`` result for that category.
"""


def publish_category_changed(category_id, revision):
    """Emit ``category.changed`` for a mutated category (or feature affecting it).

    Deliberately does NOT swallow failures: ``emit`` runs inside the mutating
    ``save()``'s atomic block, so a raise rolls the mutation back. That is the
    whole point of the transactional outbox — the event and the row it
    describes commit together or not at all. Swallowing here would let a
    mutation commit with no event, leaving every downstream
    ``categories.features`` cache silently stale (a cache-invalidation
    contract can't tolerate a lost invalidation). Let it propagate.
    """
    from stapel_core.comm import emit

    emit(
        "category.changed",
        {"category_id": int(category_id), "revision": int(revision or 0)},
        key=str(category_id),
    )
