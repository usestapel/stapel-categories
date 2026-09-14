"""``derive_axis_roles`` — fill ``Feature.axis_role_derived`` from the slug
rule table, and report what it decided.

``load_catalog`` already re-derives at the end of every run, so this command
is not how the column normally gets filled. It exists because a derivation
that only ever happens inside a catalogue load cannot be INSPECTED: an
operator asking "which leaves does the storefront think have a make, and why
not this one" had to read the column out of a shell. So the same decision is
available on its own, dry by default, with the counts printed by slug and by
resulting role.

    python manage.py derive_axis_roles              # report, write nothing
    python manage.py derive_axis_roles --dry-run    # the same, said out loud
    python manage.py derive_axis_roles --apply      # write the cache
    python manage.py derive_axis_roles --changed    # only the rows that move

The authored ``Feature.axis_role`` is never read, never written and never
overwritten here — it is what a fixture, the admin or ``set_axis_role`` said,
and :attr:`Feature.resolved_axis_role` prefers it over anything below. This
command writes exactly one column, ``axis_role_derived``.

Re-runnable by construction: the plan lists only rows whose stored value would
CHANGE, so a second run against a settled catalogue reports zero and writes
nothing.
"""
from django.core.management.base import BaseCommand, CommandError

from ...axis_roles import plan_axis_roles


class Command(BaseCommand):
    help = (
        "Derive `axis_role_derived` from the feature slug table. Dry by "
        "default; `--apply` writes. Never touches the authored `axis_role`."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the derived values. Without it the run only reports.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "Report and write nothing — the default, spelled out. Refused "
                "together with --apply."
            ),
        )
        parser.add_argument(
            "--changed",
            action="store_true",
            help="List only the slugs whose stored value would change.",
        )

    def handle(self, *args, **options):
        if options["apply"] and options["dry_run"]:
            raise CommandError("--apply and --dry-run ask for opposite things.")
        apply_changes = options["apply"]

        # The library's own plan, not a second copy of its arithmetic: this is
        # the object `--apply` writes from, so the counts below cannot drift
        # from what a write would do.
        plan = plan_axis_roles()

        self._print_by_slug(plan, only_changed=options["changed"])
        self._print_by_role(plan)
        self._print_ambiguities(plan)

        if not plan.changes:
            self.stdout.write("Nothing to write — the derivation is settled.")
            return

        if not apply_changes:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run — {plan.changes} feature row(s) would change. "
                    "Re-run with --apply to write them."
                )
            )
            return

        from ...models import Feature

        for role, pks in plan.writes.items():
            # A targeted UPDATE, not `save()`: this column is a derivation
            # cache, and a catalogue-wide re-derivation through the revision
            # bump would invalidate every downstream feature cache in the
            # fleet to record a hint. Same choice `derive_children_as` makes.
            Feature.objects.filter(pk__in=pks).update(axis_role_derived=role)
        self.stdout.write(
            self.style.SUCCESS(f"Wrote {plan.changes} feature row(s).")
        )

    def _print_by_slug(self, plan, only_changed: bool) -> None:
        changing = {pk for pks in plan.writes.values() for pk in pks}
        moving_slugs = set()
        if only_changed:
            from ...models import Feature

            moving_slugs = set(
                Feature.objects.filter(pk__in=changing).values_list(
                    "slug", flat=True
                )
            )

        rows = [
            (slug, role, count)
            for (slug, role), count in plan.rows_by_slug.items()
            # A slug deriving nothing is the bulk of any catalogue and says
            # nothing an operator can act on; the interesting silence is a
            # slug that derives a role on SOME rows and not others, and that
            # one still prints, under its own ("") line.
            if role or slug in plan.decided
        ]
        if only_changed:
            rows = [row for row in rows if row[0] in moving_slugs]
        if not rows:
            self.stdout.write("No slug derives an axis role.")
            return

        header = f"{'ROWS':>6}  {'ROLE':<12} SLUG"
        self.stdout.write("by slug")
        self.stdout.write(header)
        self.stdout.write("-" * len(header))
        for slug, role, count in sorted(rows, key=lambda r: (-r[2], r[0])):
            self.stdout.write(f"{count:>6}  {(role or '(none)'):<12} {slug}")
        self.stdout.write("")

    def _print_by_role(self, plan) -> None:
        totals = {}
        for (_slug, role), count in plan.rows_by_slug.items():
            if role:
                totals[role] = totals.get(role, 0) + count
        self.stdout.write("by role")
        if not totals:
            self.stdout.write("  (none)")
            self.stdout.write("")
            return
        for role, count in sorted(totals.items()):
            self.stdout.write(f"{count:>6}  {role}")
        self.stdout.write("")

    def _print_ambiguities(self, plan) -> None:
        if not plan.ambiguities:
            return
        self.stdout.write(
            self.style.WARNING(
                f"{len(plan.ambiguities)} ambiguit"
                f"{'y' if len(plan.ambiguities) == 1 else 'ies'} — two "
                "candidates of equal standing, so neither is derived. Pin one "
                "with `set_axis_role`, or drop the duplicate spelling."
            )
        )
        for ambiguity in plan.ambiguities:
            self.stdout.write(f"  {ambiguity}")
        self.stdout.write("")
