"""``catalog_health`` — gate on active dead-end leaves and on resurrections.

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

The second check is :func:`stapel_categories.catalog_load.active_under_inactive_parent`
— an ACTIVE category hanging off an INACTIVE one. ``active`` is stand-owned
curation and the loader writes it only on create, so a re-import cannot undo
a deactivation; but a guard protects one path, and a resurrection arriving
another way (a queryset ``.update()``, an older release applying a fixture, a
hand edit) leaves nothing for it to catch. This asserts the shape such a write
produces instead: a category a seller can reach while the path to it is
closed. A subtree retired from the top is silent here.

Usage::

    python manage.py catalog_health      # exit 0 clean, non-zero with a list

Deliberately NO ``--allow-empty-*`` escape: an allowed dead end is still a
dead end. The three real fixes — attach a feature, deactivate the leaf, or
merge it into a typed sibling — are all cheaper than remembering why an
exception list says what it says.
"""
from django.core.management.base import BaseCommand, CommandError

from stapel_categories.catalog_load import (
    active_under_inactive_parent,
    dead_end_leaves,
)


class Command(BaseCommand):
    help = (
        "List active leaf categories with zero (own + inherited) features — "
        "dead ends a seller can pick that type nothing — and active "
        "categories under an inactive parent. Non-zero exit if any."
    )

    def handle(self, *_args, **_options):
        # Both checks always run: two findings in one pass beat a gate that
        # hides the second behind the first and gets re-run to learn it.
        dead_ends = dead_end_leaves()
        resurrected = active_under_inactive_parent()

        if not dead_ends:
            self.stdout.write(
                "catalog_health: 0 dead ends — every active leaf types something."
            )
        else:
            self.stdout.write(self.style.WARNING(
                f"catalog_health: {len(dead_ends)} active leaf categor"
                f"{'y' if len(dead_ends) == 1 else 'ies'} with zero features "
                "(dead ends):"
            ))
            for slug in dead_ends:
                self.stdout.write(f"    ! {slug}")

        if not resurrected:
            self.stdout.write(
                "catalog_health: 0 active categories under an inactive parent."
            )
        else:
            self.stdout.write(self.style.WARNING(
                f"catalog_health: {len(resurrected)} active categor"
                f"{'y' if len(resurrected) == 1 else 'ies'} under an INACTIVE "
                "parent — reachable by search or link while the path is closed:"
            ))
            for slug in resurrected:
                self.stdout.write(f"    ! {slug}")

        # The slugs ride the exception too: a CI log often shows only the
        # command's stderr, and a gate that says "3 dead ends" without saying
        # which is a gate someone re-runs locally to learn what it knew.
        problems = []
        if dead_ends:
            problems.append(
                f"{len(dead_ends)} dead end(s): {', '.join(dead_ends)} — attach a "
                "feature, deactivate the leaf, or merge it into a typed sibling."
            )
        if resurrected:
            parents = self._parents_of(resurrected)
            problems.append(
                f"{len(resurrected)} active categor"
                f"{'y' if len(resurrected) == 1 else 'ies'} under an inactive "
                f"parent: {', '.join(f'{s} (under {parents[s]})' for s in resurrected)}"
                " — deactivate them too, or re-activate the parent if the "
                "subtree was meant to come back."
            )
        if problems:
            raise CommandError(" ".join(problems))

    @staticmethod
    def _parents_of(slugs):
        """``{slug: parent slug}`` — a finding names the row it hangs off."""
        from stapel_categories.models import Category

        rows = Category.objects.filter(slug__in=slugs).select_related("tn_parent")
        return {r.slug: (r.tn_parent.slug if r.tn_parent else "") for r in rows}
