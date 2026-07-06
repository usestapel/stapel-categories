"""``export_catalog`` — write the live catalog to byte-stable JSON fixtures.

CAT-1 of ``docs/catalog-fixtures-sync.md``. Snapshots ``Category`` /
``Feature`` / ``CategoryFeature`` into natural-key fixtures in the host
project's ``<BASE_DIR>/fixtures/catalog/`` (the ``staff_group`` precedent),
plus a tooling ``.sync-state.json`` sidecar (content-hash per natural key)
that CAT-2's ``load_catalog`` uses as the 3-way-diff base.

Usage::

    python manage.py export_catalog                 # -> <BASE_DIR>/fixtures/catalog/
    python manage.py export_catalog --out ./cat      # custom directory
    python manage.py export_catalog --dry-run        # report, write nothing
    python manage.py export_catalog --include-test   # local debug dump only
    python manage.py export_catalog --force          # ignore the revision pre-filter
"""
import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from stapel_categories import catalog_fixtures as cf


class Command(BaseCommand):
    help = "Export the live catalog (Category/Feature) to byte-stable JSON fixtures."

    def add_arguments(self, parser):
        parser.add_argument(
            "--out",
            dest="out",
            default=None,
            help="Output directory (default: <BASE_DIR>/fixtures/catalog/).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report create/update/delete/skip per file without writing.",
        )
        parser.add_argument(
            "--include-test",
            action="store_true",
            help="Include is_test rows (local inspection only — NOT for commit).",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Ignore the max(revision) pre-filter and always re-export.",
        )

    def get_out_dir(self, options) -> str:
        if options.get("out"):
            return options["out"]
        base_dir = getattr(settings, "BASE_DIR", ".")
        return os.path.join(str(base_dir), "fixtures", cf.FIXTURE_DIRNAME)

    def handle(self, *_args, **options):
        include_test = options["include_test"]
        dry_run = options["dry_run"]
        force = options["force"]
        out_dir = self.get_out_dir(options)

        if include_test:
            # An inspection dump must never land in the canonical fixture
            # directory: it would clobber the committed fixtures AND the
            # .sync-state.json sidecar with test-polluted hashes, corrupting
            # the 3-way base for every subsequent load_catalog.
            if not options.get("out") and not dry_run:
                raise CommandError(
                    "--include-test requires an explicit --out directory "
                    "(refusing to overwrite the canonical fixtures with a "
                    "test-data dump)."
                )
            self.stdout.write(self.style.WARNING(
                "--include-test: is_test rows are INCLUDED. This dump is for local "
                "inspection only and must NOT be committed as a catalog fixture."
            ))

        state_path = os.path.join(out_dir, cf.STATE_FILE)
        prev_state = self._read_state(state_path)

        # Pre-filter (§3.1): a pure optimization, not a source of truth. If the
        # live max(revision) has not moved since the last export and the output
        # already exists, there is nothing new to write. Skipped for
        # --include-test (changes what is emitted), --dry-run and --force.
        if (
            prev_state is not None
            and not force
            and not include_test
            and not dry_run
            and os.path.exists(os.path.join(out_dir, cf.CATEGORIES_FILE))
        ):
            from stapel_categories.models import Category, Feature
            current_max = max(Category.get_max_revision(), Feature.get_max_revision())
            if current_max == prev_state.get("max_revision"):
                self.stdout.write(
                    "No catalog changes since last export (max revision "
                    f"{current_max}); nothing to write. Use --force to re-export."
                )
                return

        features, categories, state = cf.build_catalog(include_test=include_test)

        if dry_run:
            self._report_dry_run(features, categories, state, prev_state, out_dir)
            return

        os.makedirs(out_dir, exist_ok=True)
        self._write(os.path.join(out_dir, cf.FEATURES_FILE), cf.canonical_json(features))
        self._write(os.path.join(out_dir, cf.CATEGORIES_FILE), cf.canonical_json(categories))
        self._write(state_path, cf.canonical_json(state))

        self.stdout.write(self.style.SUCCESS(
            f"Exported {len(categories)} categories and {len(features)} features to {out_dir}"
        ))

    # -- helpers -----------------------------------------------------------

    def _write(self, path: str, text: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def _read_state(self, state_path: str):
        import json
        if not os.path.exists(state_path):
            return None
        try:
            with open(state_path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def _report_dry_run(self, features, categories, state, prev_state, out_dir) -> None:
        prev_state = prev_state or {}
        self.stdout.write(f"[dry-run] target: {out_dir}")
        for label, key, records in (
            ("features", "features", features),
            ("categories", "categories", categories),
        ):
            prev_hashes = prev_state.get(key, {}) if isinstance(prev_state, dict) else {}
            new_hashes = state[key]
            created = sorted(set(new_hashes) - set(prev_hashes))
            removed = sorted(set(prev_hashes) - set(new_hashes))
            updated = sorted(
                s for s in new_hashes
                if s in prev_hashes and new_hashes[s] != prev_hashes[s]
            )
            skipped = len(new_hashes) - len(created) - len(updated)
            self.stdout.write(
                f"[dry-run] {label}: {len(records)} total — "
                f"create {len(created)}, update {len(updated)}, "
                f"delete {len(removed)}, skip {skipped}"
            )
            for s in created:
                self.stdout.write(f"    + {s}")
            for s in updated:
                self.stdout.write(f"    ~ {s}")
            for s in removed:
                self.stdout.write(f"    - {s}")
        self.stdout.write("[dry-run] no files written.")
