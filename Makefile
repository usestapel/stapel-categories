PYTHON ?= python3

.PHONY: migration-lint contract contract-check

# Expand/contract gate for Django migrations (release-management.md §3;
# stapel_tools.migration_lint). Requires stapel-tools importable (the
# workspace venv, or `pip install stapel-tools` once published).
migration-lint:
	$(PYTHON) -m stapel_tools.migration_lint . --strict

# stapel-categories — contract emission + drift gate (contract-pipeline.md §2-3).
#
# This module emits its own contract triad (schema.json + flows.json +
# errors.json) from a single-module {categories + core} Django instance
# mounted at the canonical /categories/api/v1 prefix (see _codegen.py /
# _codegen_settings.py / codegen_urls.py) — the same mechanism stapel-search,
# stapel-chat and stapel-forms already use. Emission is pinned to Python
# 3.12: drf-spectacular renders component descriptions differently across
# minors, and a contract emitted on the wrong one produces false diffs
# forever.
#
# docs/capabilities.json remains otherwise HAND-WRITTEN (authored in the
# stapel-catalog sweep, commit 1c69898 "docs: author capabilities.json for
# the stapel-catalog sweep") — no gate registry, so provides/axes/
# extension_points/requires stay verbatim. It DOES have a derived `surface`
# section (discoverability-design.md §1.2): the feature-editor engine, the
# catalog fixture-sync engine and the translation-key/display-label helpers
# a product is meant to call instead of writing its own. `stapel_tools.surface
# . --patch` refreshes ONLY module/version + `surface` from
# docs/capabilities.meta.json.
#
# docs/llms.txt (the fifth contract artifact) is rendered from the patched
# capabilities.json AND the triad above (llms_txt picks up schema/errors/
# flows automatically when present). The budget is raised from the
# generator's default 4000 to 5000 once the errors + operations sections are
# in the mix — the same deliberate exception stapel-forms (5000),
# stapel-recordings (5000) and stapel-workspaces (4500) already take. Do NOT
# shorten the `intent` lines in docs/capabilities.meta.json to fit instead —
# a trimmed context file reads exactly like a complete one at the point of
# use, which is the failure the hard budget exists to prevent.
#
# README.md is the SIXTH artifact (tracker #257): assembled by
# stapel_tools.readme from docs/readme.md (the human half — what this module
# is, how to think about it) plus everything emitted above. Badges, version,
# surface counts and doc links are generated, so a release cannot leave them
# behind. Edit docs/readme.md; never README.md.
contract:
	$(PYTHON) -m stapel_categories._codegen --out docs
	$(PYTHON) -m stapel_tools.surface . --patch
	$(PYTHON) -m stapel_tools.llms_txt . --out docs --budget 5000
	$(PYTHON) -m stapel_tools.readme .

# Drift gate: regenerate the triad into a temp dir and diff against the
# committed docs/*, then run the existing surface/llms.txt/README checks.
contract-check:
	@tmp=$$(mktemp -d); \
	$(PYTHON) -m stapel_categories._codegen --out "$$tmp" || { rm -rf "$$tmp"; exit 1; }; \
	rc=0; \
	for f in schema.json flows.json errors.json; do \
		if ! diff -q "docs/$$f" "$$tmp/$$f" >/dev/null 2>&1; then \
			echo "DRIFT: docs/$$f is stale — run 'make contract' and commit it"; \
			diff "docs/$$f" "$$tmp/$$f" | head -20; rc=1; \
		fi; \
	done; \
	rm -rf "$$tmp"; \
	$(PYTHON) -m stapel_tools.surface . --patch --check || rc=1; \
	$(PYTHON) -m stapel_tools.llms_txt . --check --budget 5000 || rc=1; \
	$(PYTHON) -m stapel_tools.readme . --check || rc=1; \
	if [ $$rc -eq 0 ]; then echo "contract-check: docs/{schema,flows,errors,capabilities,llms.txt} + README.md up to date"; fi; \
	exit $$rc
