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

The third check is :func:`stapel_categories.axis_roles.find_ambiguities` — a
category offering two candidates for one classified axis (``brand`` AND
``vendor``), where the derivation refuses to guess and therefore stamps
NEITHER. Reported as a WARNING and not a gate: the cost is a degraded
storefront link, not a listing nothing validates, and a catalogue may spell
one axis twice for reasons of its own. Its silence would otherwise be
indistinguishable from "this catalogue has no make".

Usage::

    python manage.py catalog_health      # exit 0 clean, non-zero with a list

Deliberately NO ``--allow-empty-*`` escape: an allowed dead end is still a
dead end. The three real fixes — attach a feature, deactivate the leaf, or
merge it into a typed sibling — are all cheaper than remembering why an
exception list says what it says.
"""
from django.core.management.base import BaseCommand, CommandError

from stapel_categories.axis_roles import find_ambiguities
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
        ambiguities = find_ambiguities()

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

        # A WARNING, not a gate: a leaf that names no make is a degraded
        # storefront link, not a seller filing a listing nothing validates,
        # and turning it into a non-zero exit would fail every deployment
        # whose catalogue spells one axis twice for reasons of its own. It is
        # still worth saying out loud — the derivation's silence is otherwise
        # indistinguishable from "this catalogue has no make".
        if ambiguities:
            self.stdout.write(self.style.WARNING(
                f"catalog_health: {len(ambiguities)} axis-role ambiguit"
                f"{'y' if len(ambiguities) == 1 else 'ies'} — two features "
                "of EQUAL precedence claim one axis in one category (or one "
                "shared row would have to answer two ways), so neither is "
                "derived (pin one with `set_axis_role`, or drop the duplicate "
                "spelling):"
            ))
            for ambiguity in ambiguities:
                self.stdout.write(self.style.WARNING(f"    ! {ambiguity}"))
        else:
            self.stdout.write(
                "catalog_health: 0 axis-role ambiguities — every category "
                "that names an axis names exactly one feature for it."
            )

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
