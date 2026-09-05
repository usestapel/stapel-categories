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

This command also owns ``children_axis_label`` — the NAME of the axis a chip
row splits on («Тип жилья» over Новостройка | Вторичка). ``derive_children_as``
only ever fills a BLANK label from the vocabulary group it matched, or
improves its own previous key; a caption an engineer actually wrote is
authored text with no command of its own until now, and had to be edited as
fixture data::

    python manage.py set_children_as --path a/b --axis-label "Тип жилья"
    python manage.py set_children_as --path a/b --clear-axis-label

``--value`` and ``--axis-label``/``--clear-axis-label`` combine freely in one
run (one write per node either way); at least one of the three must be given.
``--axis-label``/``--clear-axis-label`` write the column exactly as given —
including a value ``derive_children_as`` would never have emitted itself —
because this is the authoring side of the same column, not a re-derivation.

Writes go through ``Category.save()``, not a targeted UPDATE: this is authored
content a reader sees, so the revision bump and the ``category.changed`` event
that invalidate every downstream ``categories.features`` cache are the point.
(``derive_children_as`` writes its CACHE columns with a bare UPDATE for the
opposite reason — a catalogue-wide re-derivation must not fan out.)

Nothing is written for a node that already carries the value(s) given; the
run says so per path and in the summary, so a re-run of the same list prints
"0 changed" rather than churning revisions.
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
            default=None,
            choices=list(VALUES),
            help=(
                f"The authored `children_as` value to write: "
                f"{' | '.join(VALUES)}. Optional if --axis-label or "
                "--clear-axis-label is given instead (or as well)."
            ),
        )
        axis_group = parser.add_mutually_exclusive_group()
        axis_group.add_argument(
            "--axis-label",
            type=str,
            default=None,
            help=(
                "Authored `children_axis_label` text to write on the named "
                "node(s) — the name of the axis a chip row splits on (e.g. "
                "«Тип жилья»). `derive_children_as` never "
                "overwrites text set here."
            ),
        )
        axis_group.add_argument(
            "--clear-axis-label",
            action="store_true",
            help="Blank the authored `children_axis_label` on the named node(s).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would change and write nothing.",
        )

    def handle(self, *args, **options):
        value = options["value"]
        clear_axis_label = options["clear_axis_label"]
        set_axis_label = options["axis_label"] is not None or clear_axis_label
        # "" for --clear-axis-label, the given text for --axis-label, or
        # None when this run does not touch the column at all — three states,
        # not two, because "" is itself a value this command can write.
        new_axis_label = "" if clear_axis_label else options["axis_label"]

        if value is None and not set_axis_label:
            raise CommandError(
                "Give --value, --axis-label or --clear-axis-label (or several)."
            )

        paths = list(options["path"])
        if options["paths_from"]:
            paths.extend(self._read_paths(options["paths_from"]))
        if not paths:
            raise CommandError("Give at least one --path (or --paths-from)")

        # Resolve EVERY path before writing any of them: a list with one typo
        # in it must not leave half the census applied.
        targets = [(path, resolve_path(path)) for path in paths]

        # One plan per path, computed before anything is printed or written,
        # so `--value chips --axis-label "..."` writes both columns in the
        # SAME save — one revision bump per node, not two.
        plans = []
        for path, node in targets:
            value_changes = value is not None and node.children_as != value
            label_changes = (
                set_axis_label and node.children_axis_label != new_axis_label
            )
            plans.append((path, node, value_changes, label_changes))

        for path, node, value_changes, label_changes in plans:
            pieces = []
            if value is not None:
                if value_changes:
                    pieces.append(f"children_as {node.children_as} -> {value}")
                else:
                    pieces.append(f"children_as unchanged ({value})")
            if set_axis_label:
                before = node.children_axis_label or "(empty)"
                after = new_axis_label or "(empty)"
                if label_changes:
                    pieces.append(f"axis_label {before!r} -> {after!r}")
                else:
                    pieces.append(f"axis_label unchanged ({after!r})")
            tag = "set" if (value_changes or label_changes) else "unchanged"
            self.stdout.write(f"{tag:<10} {'  '.join(pieces)}  {path}")

        changed = [plan for plan in plans if plan[2] or plan[3]]
        if not changed:
            self.stdout.write(
                f"Nothing to write — {len(plans)} path(s) already as given."
            )
            return

        if options["dry_run"]:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run — {len(changed)} of {len(plans)} path(s) would "
                    "change. Re-run without --dry-run to write them."
                )
            )
            return

        with transaction.atomic():
            for _path, node, value_changes, label_changes in changed:
                if value_changes:
                    node.children_as = value
                if label_changes:
                    node.children_axis_label = new_axis_label
                # A full save, deliberately: see the module docstring.
                node.save()

        self.stdout.write(
            self.style.SUCCESS(f"Wrote {len(changed)} of {len(plans)} path(s).")
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
