"""``catalog_health`` — gate on active dead-end leaves.

A dead end is an ACTIVE, non-deleted LEAF category (no active, non-deleted
children) with ZERO features, own or inherited: a seller can pick it, and it
types nothing — no form, no validation, no facet. A live classified stand
imported a catalogue whose untyped scraps landed exactly like that, and the
first report came from sellers, not from tooling.

The finder is :func:`stapel_categories.catalog_load.dead_end_leaves`, which
resolves features with the library's own inheritance logic
(``Category.get_all_features``) — this command cannot disagree with the form
the product renders. ``load_catalog`` surfaces the same count in its report
summary at import time; this command is the standing gate.

Usage::

    python manage.py catalog_health      # exit 0 clean, non-zero with a list

Deliberately NO ``--allow-empty-*`` escape: an allowed dead end is still a
dead end. The three real fixes — attach a feature, deactivate the leaf, or
merge it into a typed sibling — are all cheaper than remembering why an
exception list says what it says.
"""
from django.core.management.base import BaseCommand, CommandError

from stapel_categories.catalog_load import dead_end_leaves


class Command(BaseCommand):
    help = (
        "List active leaf categories with zero (own + inherited) features — "
        "dead ends a seller can pick that type nothing. Non-zero exit if any."
    )

    def handle(self, *_args, **_options):
        slugs = dead_end_leaves()
        if not slugs:
            self.stdout.write("catalog_health: 0 dead ends — every active leaf types something.")
            return
        self.stdout.write(self.style.WARNING(
            f"catalog_health: {len(slugs)} active leaf categor"
            f"{'y' if len(slugs) == 1 else 'ies'} with zero features (dead ends):"
        ))
        for slug in slugs:
            self.stdout.write(f"    ! {slug}")
        # The slugs ride the exception too: a CI log often shows only the
        # command's stderr, and a gate that says "3 dead ends" without saying
        # which is a gate someone re-runs locally to learn what it knew.
        raise CommandError(
            f"{len(slugs)} dead end(s): {', '.join(slugs)} — attach a "
            "feature, deactivate the leaf, or merge it into a typed sibling."
        )
