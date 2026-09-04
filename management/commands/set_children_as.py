"""``set_children_as`` — author ``children_as`` on named nodes, from a list.

``transparent`` (and, for the same reason, a hand-pinned ``tiles``/``chips``)
is a decision made by reading a catalogue, not by looking at one node: an
engineer walks the census, writes down the paths, and applies them. Doing that
through the admin is one changeform per node — 34 of them for one services
wrapper — and doing it through a DB console leaves no record of what changed.
So: one command, a path per node, idempotent, and it PRINTS what it changed.

Usage::

    python manage.py set_children_as --path uslugi/predlozhenie-uslug \\
        --value transparent
    python manage.py set_children_as --value tiles \\
        --path a/b --path a/c        # repeat --path for a whole list
    python manage.py set_children_as --value transparent --paths-from list.txt

A path is the SLUG path root->self, the exact form ``derive_children_as``
prints in its report, so a census read off that report can be pasted back
here. A bare slug is accepted too (the column is unique); a longer path is
CHECKED against the tree, and a path that no longer matches is refused rather
than applied to a node that has been re-parented since the census was taken.

Writes go through ``Category.save()``, not a targeted UPDATE: this is authored
content a reader sees, so the revision bump and the ``category.changed`` event
that invalidate every downstream ``categories.features`` cache are the point.
(``derive_children_as`` writes its CACHE column with a bare UPDATE for the
opposite reason — a catalogue-wide re-derivation must not fan out.)

Nothing is written for a node that already carries the value; the run says so
per path and in the summary, so a re-run of the same list prints "0 changed"
rather than churning revisions.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from ...models import CHILDREN_AS_AUTHORED_CHOICES, Category

#: The values an operator may set here — exactly the model's authored set.
VALUES = tuple(value for value, _ in CHILDREN_AS_AUTHORED_CHOICES)


def resolve_path(path: str) -> Category:
    """The category a slug path names, or a :class:`CommandError` saying why.

    ``Category.slug`` is unique, so the LAST segment already identifies the
    row. The leading segments are still read — as an assertion that the path
    the engineer wrote is the tree the fleet has. A census pasted out of a
    stale ``derive_children_as`` report would otherwise apply itself silently
    to a node that has since been re-parented, which is exactly the kind of
    edit nobody notices until a menu draws the wrong level.
    """
    segments = [segment for segment in path.strip().strip("/").split("/") if segment]
    if not segments:
        raise CommandError("Empty --path")

    try:
        node = Category.objects.get(slug=segments[-1], deleted=False)
    except Category.DoesNotExist:
        raise CommandError(f"No category with slug {segments[-1]!r} (path {path!r})")

    if len(segments) > 1:
        ancestors = [
            Category.objects.filter(pk=int(pk)).values_list("slug", flat=True).first()
            for pk in node.get_ancestors_pks()
        ]
        if ancestors != segments[:-1]:
            actual = "/".join([*(a or "?" for a in ancestors), node.slug])
            raise CommandError(
                f"Path {path!r} does not match the tree — {node.slug!r} is at "
                f"{actual!r}"
            )
    return node


class Command(BaseCommand):
    help = (
        "Set the authored `children_as` on categories named by slug path. "
        "Idempotent; prints what changed."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--path",
            action="append",
            default=[],
            help=(
                "Slug path root->self of a category to set (e.g. "
                "`uslugi/predlozhenie-uslug`). Repeat for several."
            ),
        )
        parser.add_argument(
            "--paths-from",
            type=str,
            default="",
            help="File with one slug path per line ('#' comments, blanks ignored).",
        )
        parser.add_argument(
            "--value",
            required=True,
            choices=list(VALUES),
            help=f"The authored value to write: {' | '.join(VALUES)}.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change and write nothing.",
        )

    def handle(self, *args, **options):
        value = options["value"]
        paths = list(options["path"])
        if options["paths_from"]:
            paths.extend(self._read_paths(options["paths_from"]))
        if not paths:
            raise CommandError("Give at least one --path (or --paths-from)")

        # Resolve EVERY path before writing any of them: a list with one typo
        # in it must not leave half the census applied.
        targets = [(path, resolve_path(path)) for path in paths]

        changes = [
            (path, node) for path, node in targets if node.children_as != value
        ]
        for path, node in targets:
            before = node.children_as
            if before == value:
                self.stdout.write(f"unchanged  {value:<11}  {path}")
            else:
                self.stdout.write(f"set        {before} -> {value:<11}  {path}")

        if not changes:
            self.stdout.write(f"Nothing to write — {len(targets)} path(s) already {value}.")
            return

        if options["dry_run"]:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run — {len(changes)} of {len(targets)} path(s) would "
                    "change. Re-run without --dry-run to write them."
                )
            )
            return

        with transaction.atomic():
            for _path, node in changes:
                node.children_as = value
                # A full save, deliberately: see the module docstring.
                node.save()

        self.stdout.write(
            self.style.SUCCESS(
                f"Wrote {len(changes)} of {len(targets)} path(s) as {value}."
            )
        )

    @staticmethod
    def _read_paths(filename: str) -> list[str]:
        try:
            with open(filename, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except OSError as exc:
            raise CommandError(f"Cannot read {filename!r}: {exc}")
        return [
            line.strip()
            for line in lines
            if line.strip() and not line.strip().startswith("#")
        ]
