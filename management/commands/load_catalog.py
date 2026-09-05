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

Records are matched by **source identity first** — a fixture row carrying
``external_id`` finds its live row by ``(external_source, external_id)``, and
only a row without one falls back to the slug. A source-side rename therefore
updates in place and is reported as ``» slug 'a' → 'b' (external_id 'X')``,
distinct from the ``+``/``-`` of an add or a removal.

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
    cl.CONFLICT, cl.DB_ONLY, cl.DB_NEW, cl.DB_NEW_IN_CANON,
    cl.NAME_COLLISION, cl.RESIDUAL, cl.RENAME_BLOCKED, cl.ERROR,
)
_KIND_LABEL = {
    cl.CREATED: "created",
    cl.UPDATED: "updated",
    cl.DELETED: "deleted",
    cl.SKIPPED: "skipped",
    cl.CONFLICT: "CONFLICT",
    cl.DB_ONLY: "db-only drift",
    cl.DB_NEW: "db-only (not in canon)",
    cl.DB_NEW_IN_CANON: "db-only INSIDE canon subtree",
    cl.NAME_COLLISION: "sibling name collision",
    cl.RESIDUAL: "applied but not equal to canon",
    cl.RENAME_BLOCKED: "feature rename BLOCKED",
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
    cl.DB_NEW_IN_CANON: "?",
    cl.NAME_COLLISION: "?",
    cl.RESIDUAL: "≈",
    cl.RENAME_BLOCKED: "»",
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
            "--rename-features",
            action="store_true",
            help=(
                "Apply feature SLUG renames, and hand the stored answers to the "
                "FEATURE_RENAME_HOOK so they move with the schema. Without this "
                "flag a detected rename is reported and the live slug is kept: "
                "a slug is the key every listing files its answer under, so "
                "moving it here alone strands every one of them."
            ),
        )
        parser.add_argument(
            "--no-hook",
            action="store_true",
            help=(
                "With --rename-features: rename the slugs WITHOUT calling the "
                "hook. The explicit statement that no listings stand behind this "
                "catalogue — say it out loud rather than by omission."
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
                rename_features=options["rename_features"],
                call_hook=not options["no_hook"],
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
            # Renames are a subset of the updates, and the number an operator
            # reading a catalogue re-sync plan actually wants: it is the count of
            # rows that would have been an add + a remove under slug matching.
            renamed = sum(1 for it in items if it.renamed)
            if renamed:
                summary += f" (of which renamed {renamed})"
            self.stdout.write(f"{prefix}{label}: {summary}")
            for it in items:
                if it.kind == cl.SKIPPED and not it.detail:
                    continue  # keep the noise down; counts above cover it
                mark = "»" if it.renamed else _KIND_MARK[it.kind]
                line = f"    {mark} {it.key}"
                if it.detail:
                    line += f"  ({it.detail})"
                if it.kind in (cl.CONFLICT, cl.ERROR):
                    self.stdout.write(self.style.ERROR(line))
                elif it.kind in (
                    cl.DB_ONLY, cl.DB_NEW, cl.DB_NEW_IN_CANON, cl.NAME_COLLISION,
                    cl.RESIDUAL, cl.RENAME_BLOCKED,
                ):
                    self.stdout.write(self.style.WARNING(line))
                else:
                    self.stdout.write(line)
        if report.kept_unsaid:
            # What the load did NOT blank. An absent key is not an instruction
            # to empty a column, and the erasure this replaces was silent: a
            # reload wiped every axis caption `derive_children_as` had written
            # and reported nothing but "updated".
            total = sum(report.kept_unsaid.values())
            detail = ", ".join(
                f"{key} {count}" for key, count in sorted(report.kept_unsaid.items())
            )
            self.stdout.write(
                f"{prefix}kept {total} live value(s) the fixture does not "
                f"state ({detail})"
            )
        self._print_renames(report, prefix)
        if report.dead_end_leaves:
            # Import-time echo of catalog_health's gate: the tree this load
            # just produced has active leaves that type nothing. The load
            # itself did what it was asked; whether this blocks a deploy is
            # catalog_health's non-zero exit, not this command's.
            slugs = ", ".join(report.dead_end_leaves)
            self.stdout.write(self.style.WARNING(
                f"{len(report.dead_end_leaves)} active leaf categor"
                f"{'y' if len(report.dead_end_leaves) == 1 else 'ies'} now "
                f"type(s) nothing — dead end(s): {slugs} "
                "(run catalog_health for the standing gate)"
            ))
        if report.resurrected:
            # A load cannot cause this any more (`active` is create-only), so
            # one here says a resurrection reached the tree another way.
            slugs = ", ".join(report.resurrected)
            self.stdout.write(self.style.WARNING(
                f"{len(report.resurrected)} active categor"
                f"{'y' if len(report.resurrected) == 1 else 'ies'} under an "
                f"INACTIVE parent: {slugs} "
                "(run catalog_health for the standing gate)"
            ))
        if report.dry_run:
            self.stdout.write("[dry-run] no changes were written.")

    def _print_renames(self, report: cl.Report, prefix: str) -> None:
        """The line the 2026-09-05 import never printed.

        A feature slug is the key every listing files its answer under. The run
        that renamed five of them said ``features: updated 62`` and nothing
        else, and the answers were stranded before anybody knew a rename had
        happened. So: named, counted, and told which half of the migration ran.
        """
        if not report.feature_renames:
            return
        pairs = ", ".join(
            f"{old} → {new}" for old, new in sorted(report.feature_renames.items())
        )
        categories = len(report.feature_renames_by_category)
        line = (
            f"{prefix}feature renames: {len(report.feature_renames)} "
            f"({pairs}) across {categories} categor"
            f"{'y' if categories == 1 else 'ies'}"
        )
        if not report.feature_renames_applied:
            self.stdout.write(self.style.WARNING(
                f"{line} — NOT applied. Every listing in those categories still "
                "answers under the old slug; re-run with --rename-features to "
                "move the schema and the stored answers together."
            ))
            return
        self.stdout.write(f"{line} — applied.")
        if not report.rename_hook:
            self.stdout.write(self.style.WARNING(
                "  no rename hook was called (--no-hook): the stored answers "
                "were NOT moved. Run the listings-side rename by hand."
            ))
            return
        self.stdout.write(f"  {report.rename_hook}:")
        for entry in report.rename_hook_results:
            if "error" in entry:
                self.stdout.write(self.style.ERROR(
                    f"    {entry['category']}: FAILED — {entry['error']} "
                    f"(replay with renames {entry['renames']})"
                ))
                continue
            result = entry.get("result") or {}
            self.stdout.write(
                f"    {entry['category']}: {result.get('keys_renamed', 0)} key(s) "
                f"moved across {result.get('listings_changed', 0)} listing(s), "
                f"{len(result.get('conflicts') or [])} conflict(s)"
            )
