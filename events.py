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

    Uses ``mutate_and_emit`` (stapel-core >= 0.3.3): called from the mutating
    ``save()``'s atomic block it joins that transaction, so the event and the
    row it describes commit together or not at all. Failures are deliberately
    NOT swallowed — core marks the transaction rollback-only on a failed emit,
    so a swallowed failure could not commit anyway (review C1). A lost
    invalidation would leave every downstream ``categories.features`` cache
    silently stale.
    """
    from stapel_core.comm import mutate_and_emit

    # savepoint=False: when joining the save()'s transaction there is nothing
    # to partially roll back to — a failed emit must sink the whole mutation.
    # Also keeps the N-fanout on Feature save free of N savepoints.
    with mutate_and_emit(savepoint=False) as emit_event:
        emit_event(
            "category.changed",
            {"category_id": int(category_id), "revision": int(revision or 0)},
            key=str(category_id),
        )
