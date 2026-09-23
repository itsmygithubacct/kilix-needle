PYTHON ?= python3

.PHONY: all test eval
# What the Kilix catalog runs after fetching a pinned checkout (submodules
# included). Nothing is compiled: this proves the checkout can run.
all:
	@$(PYTHON) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "kilix-needle needs Python 3.11 or newer")'
	@test -f third_party/kilix-content/src/kilix_content/__init__.py || \
		{ echo "third_party/kilix-content is missing: run git submodule update --init"; exit 1; }
	@$(PYTHON) -B -c 'import needle_cli, mcp_server, tuning' && echo "kilix-needle: ready"

test:
	$(PYTHON) -B -m unittest discover -s tests -v

eval:
	$(PYTHON) -B evaluate.py evals/commands.jsonl
