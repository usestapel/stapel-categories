PYTHON ?= python3

.PHONY: migration-lint contract contract-check

# Expand/contract gate for Django migrations (release-management.md §3;
# stapel_tools.migration_lint). Requires stapel-tools importable (the
# workspace venv, or `pip install stapel-tools` once published).
migration-lint:
	$(PYTHON) -m stapel_tools.migration_lint . --strict

# stapel-categories — docs/llms.txt emission + drift gate (contract-pipeline.md §2-3).
#
# docs/capabilities.json here is HAND-WRITTEN (authored in the stapel-catalog
# sweep, commit 1c69898 "docs: author capabilities.json for the stapel-catalog
# sweep") — no gate registry, no docs/schema.json, nothing for a codegen step
# to derive axes/surface from. The targets below manage ONLY the fifth
# contract artifact, docs/llms.txt (stapel_tools.llms_txt), rendered straight
# from that curated capabilities.json. They do NOT regenerate or touch
# capabilities.json itself — that stays hand-edited.

# Emit docs/llms.txt from the (hand-written) docs/capabilities.json.
contract:
	$(PYTHON) -m stapel_tools.llms_txt .

# Drift gate: regenerate into a temp dir and diff against the committed docs/llms.txt.
contract-check:
	@tmp=$$(mktemp -d); \
	$(PYTHON) -m stapel_tools.llms_txt . --out "$$tmp" || { rm -rf "$$tmp"; exit 1; }; \
	if ! diff -q docs/llms.txt "$$tmp/llms.txt" >/dev/null 2>&1; then \
		echo "DRIFT: docs/llms.txt is stale — run 'make contract' and commit it"; \
		diff docs/llms.txt "$$tmp/llms.txt" | head -20; \
		rm -rf "$$tmp"; exit 1; \
	fi; \
	rm -rf "$$tmp"; \
	echo "contract-check: docs/llms.txt up to date"
