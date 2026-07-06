"""``load_catalog`` — reconcile JSON catalog fixtures into the live DB.

CAT-2 of ``docs/catalog-fixtures-sync.md``. The inverse of ``export_catalog``:
reads ``<BASE_DIR>/fixtures/catalog/`` (``features.json`` / ``categories.json``
+ the ``.sync-state.json`` sidecar) and applies a **3-way diff**
(base = sidecar, theirs = fixture files, ours = live DB) through
``Model.save()``/``full_clean()`` only — the loader earns the same side effects
as an admin/Studio edit (revision bump, ``category.changed``,
``copy_parent_features``). Engine: :mod:`stapel_categories.catalog_load`.

Usage::

    python manage.py load_catalog                      # <BASE_DIR>/fixtures/catalog/
    python manage.py load_catalog --dir ./cat          # custom directory
    python manage.py load_catalog --dry-run            # classify + report, no writes
    python manage.py load_catalog --on-conflict fixture-wins
    python manage.py load_catalog --deletions hard     # real DELETE (default: soft)
    python manage.py load_catalog --seed-if-empty      # bootstrap idiom, no-op if populated

Exit code is non-zero when any record conflicted (default per-record abort) or
failed validation — CI can gate on it. Non-conflicting records ARE applied.
"""
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from stapel_categories import catalog_fixtures as cf
from stapel_categories import catalog_load as cl

_KIND_ORDER = (
    cl.CREATED, cl.UPDATED, cl.DELETED, cl.SKIPPED,
    cl.CONFLICT, cl.DB_ONLY, cl.DB_NEW, cl.ERROR,
)
_KIND_LABEL = {
    cl.CREATED: "created",
    cl.UPDATED: "updated",
    cl.DELETED: "deleted",
    cl.SKIPPED: "skipped",
    cl.CONFLICT: "CONFLICT",
    cl.DB_ONLY: "db-only drift",
    cl.DB_NEW: "db-only (not in canon)",
    cl.ERROR: "ERROR",
}
_KIND_MARK = {
    cl.CREATED: "+",
    cl.UPDATED: "~",
    cl.DELETED: "-",
    cl.SKIPPED: "=",
    cl.CONFLICT: "!",
    cl.DB_ONLY: "?",
    cl.DB_NEW: "?",
    cl.ERROR: "E",
}


class Command(BaseCommand):
    help = (
        "Reconcile catalog JSON fixtures into the DB via a 3-way diff "
        "(sidecar base / fixture / live DB)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dir",
            dest="dir",
            default=None,
            help="Fixture directory (default: <BASE_DIR>/fixtures/catalog/).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Classify every record and print the full report without writing.",
        )
        parser.add_argument(
            "--on-conflict",
            choices=list(cl.ON_CONFLICT_CHOICES),
            default=cl.ON_CONFLICT_ABORT,
            help=(
                "Policy for records changed on BOTH sides since the last sync. "
                "abort (default): leave the record, report it, exit non-zero; "
                "fixture-wins / db-wins: resolve ALL conflicts to that side."
            ),
        )
        parser.add_argument(
            "--deletions",
            choices=list(cl.DELETIONS_CHOICES),
            default=cl.DELETIONS_SOFT,
            help=(
                "What a removal from the fixture does to the DB row. "
                "soft (default): RevisionMixin.soft_delete() (deleted=True, reversible); "
                "hard: real DELETE; ignore: never delete."
            ),
        )
        parser.add_argument(
            "--seed-if-empty",
            action="store_true",
            help=(
                "Bootstrap idiom (load_staff_group_if_empty): full load on an "
                "empty catalog, warn + no-op on a populated one."
            ),
        )

    def get_dir(self, options) -> str:
        if options.get("dir"):
            return options["dir"]
        base_dir = getattr(settings, "BASE_DIR", ".")
        return os.path.join(str(base_dir), "fixtures", cf.FIXTURE_DIRNAME)

    def handle(self, *_args, **options):
        directory = self.get_dir(options)
        # Both files are required: a missing features.json alongside a present
        # categories.json would read as "every root feature was removed from
        # the fixture" and mass-(soft-)delete the feature table.
        missing = [
            name for name in (cf.CATEGORIES_FILE, cf.FEATURES_FILE)
            if not os.path.exists(os.path.join(directory, name))
        ]
        if missing:
            raise CommandError(
                f"catalog fixtures incomplete in {directory} "
                f"(missing {', '.join(missing)}); run export_catalog first "
                "or pass --dir."
            )

        try:
            report = cl.load_catalog(
                directory,
                dry_run=options["dry_run"],
                on_conflict=options["on_conflict"],
                deletions=options["deletions"],
                seed_if_empty=options["seed_if_empty"],
            )
        except ValueError as exc:  # incompatible sidecar version
            raise CommandError(str(exc))

        self._print_report(report, directory)

        if report.failed:
            raise CommandError(
                f"{report.conflicts} conflict(s), {report.errors} error(s) — "
                "see the report above. Non-conflicting records were applied"
                + (" (dry run: nothing was written)." if report.dry_run else ".")
            )

    # -- reporting -----------------------------------------------------------

    def _print_report(self, report: cl.Report, directory: str) -> None:
        prefix = "[dry-run] " if report.dry_run else ""
        self.stdout.write(f"{prefix}load_catalog: {directory}")
        for label, items in (("features", report.features), ("categories", report.categories)):
            counts = {k: sum(1 for it in items if it.kind == k) for k in _KIND_ORDER}
            summary = ", ".join(
                f"{_KIND_LABEL[k]} {counts[k]}" for k in _KIND_ORDER if counts[k]
            ) or "nothing to do"
            self.stdout.write(f"{prefix}{label}: {summary}")
            for it in items:
                if it.kind == cl.SKIPPED and not it.detail:
                    continue  # keep the noise down; counts above cover it
                line = f"    {_KIND_MARK[it.kind]} {it.key}"
                if it.detail:
                    line += f"  ({it.detail})"
                if it.kind in (cl.CONFLICT, cl.ERROR):
                    self.stdout.write(self.style.ERROR(line))
                elif it.kind in (cl.DB_ONLY, cl.DB_NEW):
                    self.stdout.write(self.style.WARNING(line))
                else:
                    self.stdout.write(line)
        if report.dry_run:
            self.stdout.write("[dry-run] no changes were written.")
