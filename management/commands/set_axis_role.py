"""``set_axis_role`` — author ``Feature.axis_role``, overriding the derivation.

``load_catalog`` derives which feature of a leaf is the make, the model, the
year, from the slug rule table in :mod:`stapel_categories.axis_roles`. The
table is a claim about the catalogues this fleet imports, and it is wrong in
two directions that only a person can settle:

* a catalogue spells the axis a way the table has never seen
  (``proizvoditel``, a numbered field, a product-specific word) — nothing is
  derived, and the storefront quietly has no make on that leaf;
* a category offers TWO candidates for one role (``brand`` and ``vendor``
  both present) — the derivation refuses to guess and derives neither, which
  is the honest answer and still leaves the leaf without an axis.

Both are fixed the same way: name the feature and say which axis it is. The
value lands in the AUTHORED column, which :attr:`Feature.resolved_axis_role`
reads first and no derivation ever overwrites — so this is a decision, not a
hint, and a re-import cannot undo it.

Usage::

    python manage.py set_axis_role --slug vendor --role make
    python manage.py set_axis_role --slug proizvoditel --slug marka --role make
    python manage.py set_axis_role --slug body_type --clear
    python manage.py set_axis_role --slug vendor --role make --dry-run

A feature is named by ``slug`` — the same key a fixture, a listing and the
loader use, and the key an ambiguity report prints. Slug-less display rows
(``header``) cannot be named here and have no axis to be.

**Every row under the slug is written**, root and per-category overrides
alike: the role is a fact about which axis the FIELD is, not about one
category's copy of it, and stamping the root while leaving an override
unstamped is precisely the silent hole this whole field exists to close.

Writes go through ``Feature.save()``, not a targeted UPDATE: this is authored
content a reader sees, so the revision bump and the ``category.changed``
event that invalidate every downstream ``categories.features`` cache are the
point. (The derivation writes its CACHE column with a bare UPDATE for the
opposite reason — a catalogue-wide re-derivation must not fan out.)

**It does not travel until it is exported.** The authored column ships in
``features.json`` (``export_catalog``) and the loader applies what the
fixture says, so a role pinned here and never exported is overwritten by the
next ``load_catalog`` — exactly like a name or a visibility edited by hand.
Run ``export_catalog`` after a pinning session.

Nothing is written for a feature that already carries the role given; the run
says so per slug and in the summary, so a re-run prints "0 changed" rather
than churning revisions.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ...models import Feature

#: What an operator may author — exactly the model's (and the canon's) set.
ROLES = tuple(value for value, _ in Feature.AxisRole.choices)


class Command(BaseCommand):
    help = (
        "Set the authored `axis_role` on features named by slug — which "
        "classified axis the field IS. Idempotent; prints what changed."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--slug",
            action="append",
            default=[],
            help="Feature slug to set. Repeat for several.",
        )
        group = parser.add_mutually_exclusive_group(required=True)
        group.add_argument(
            "--role",
            default=None,
            choices=list(ROLES),
            help=f"The axis role to author: {' | '.join(ROLES)}.",
        )
        group.add_argument(
            "--clear",
            action="store_true",
            help=(
                "Blank the authored role, handing the feature back to the "
                "loader's derivation."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change and write nothing.",
        )

    def handle(self, *args, **options):
        slugs = [slug.strip() for slug in options["slug"] if slug.strip()]
        if not slugs:
            raise CommandError("Give at least one --slug")
        role = "" if options["clear"] else options["role"]

        # Resolve EVERY slug before writing any of them: a list with one typo
        # in it must not leave half the session applied.
        targets = []
        for slug in slugs:
            rows = list(Feature.objects.filter(slug=slug, deleted=False).order_by("pk"))
            if not rows:
                raise CommandError(f"No live feature with slug {slug!r}")
            targets.append((slug, rows))

        plans = []
        for slug, rows in targets:
            changing = [row for row in rows if row.axis_role != role]
            plans.append((slug, rows, changing))
            before = {row.axis_role or "(none)" for row in rows}
            after = role or "(none)"
            tag = "set" if changing else "unchanged"
            self.stdout.write(
                f"{tag:<10} {slug}  axis_role {'/'.join(sorted(before))} -> {after}"
                f"  ({len(changing)} of {len(rows)} row(s))"
            )

        changed = [plan for plan in plans if plan[2]]
        if not changed:
            self.stdout.write(
                f"Nothing to write — {len(plans)} slug(s) already as given."
            )
            return

        rows_to_write = sum(len(plan[2]) for plan in changed)
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING(
                f"Dry run — {rows_to_write} row(s) across {len(changed)} of "
                f"{len(plans)} slug(s) would change. Re-run without --dry-run "
                "to write them."
            ))
            return

        with transaction.atomic():
            for _slug, _rows, changing in changed:
                for row in changing:
                    row.axis_role = role
                    # A full save, deliberately: see the module docstring.
                    row.save()

        self.stdout.write(self.style.SUCCESS(
            f"Wrote {rows_to_write} row(s) across {len(changed)} of "
            f"{len(plans)} slug(s). Run `export_catalog` to carry the "
            "authored role into the fixtures."
        ))
