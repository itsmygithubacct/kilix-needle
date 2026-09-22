PYTHON ?= python3

.PHONY: test eval
test:
	$(PYTHON) -B -m unittest discover -s tests -v

eval:
	$(PYTHON) -B evaluate.py evals/commands.jsonl
