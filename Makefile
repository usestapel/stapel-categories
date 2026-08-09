PYTHON ?= python3

.PHONY: migration-lint contract contract-check

# Expand/contract gate for Django migrations (release-management.md §3;
# stapel_tools.migration_lint). Requires stapel-tools importable (the
# workspace venv, or `pip install stapel-tools` once published).
migration-lint:
	$(PYTHON) -m stapel_tools.migration_lint . --strict

# stapel-categories — docs/llms.txt emission + drift gate (contract-pipeline.md §2-3).
#
# docs/capabilities.json here is otherwise HAND-WRITTEN (authored in the
# stapel-catalog sweep, commit 1c69898 "docs: author capabilities.json for
# the stapel-catalog sweep") — no gate registry, no docs/schema.json for a
# codegen step to derive axes from. It DOES now have a derived `surface`
# section (discoverability-design.md §1.2): the feature-editor engine, the
# catalog fixture-sync engine and the translation-key/display-label helpers
# a product is meant to call instead of writing its own. `stapel_tools.surface
# . --patch` refreshes ONLY module/version + `surface` from
# docs/capabilities.meta.json, leaving provides/axes/extension_points/requires
# verbatim. Then docs/llms.txt (the fifth contract artifact) is rendered from
# the patched document.

# Patch `surface` (+ module/version) into docs/capabilities.json, then emit
# docs/llms.txt from the result.
#
# README.md is the SIXTH artifact (tracker #257): assembled by
# stapel_tools.readme from docs/readme.md (the human half — what this module
# is, how to think about it) plus everything emitted above. Badges, version,
# surface counts and doc links are generated, so a release cannot leave them
# behind. Edit docs/readme.md; never README.md.
contract:
	$(PYTHON) -m stapel_tools.surface . --patch
	$(PYTHON) -m stapel_tools.llms_txt .
	$(PYTHON) -m stapel_tools.readme .

# Drift gate: regenerate into a temp dir and diff against the committed docs/*.
contract-check:
	$(PYTHON) -m stapel_tools.surface . --patch --check
	@tmp=$$(mktemp -d); \
	$(PYTHON) -m stapel_tools.llms_txt . --out "$$tmp" || { rm -rf "$$tmp"; exit 1; }; \
	if ! diff -q docs/llms.txt "$$tmp/llms.txt" >/dev/null 2>&1; then \
		echo "DRIFT: docs/llms.txt is stale — run 'make contract' and commit it"; \
		diff docs/llms.txt "$$tmp/llms.txt" | head -20; \
		rm -rf "$$tmp"; exit 1; \
	fi; \
	rm -rf "$$tmp"; \
	$(PYTHON) -m stapel_tools.readme . --check || exit 1; \
	echo "contract-check: docs/llms.txt + README.md up to date"
