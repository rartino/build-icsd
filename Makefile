PYTHON ?= python3
COD_PATH ?= ../DATA/COD
FORMAT ?= sqlite
OUTPUT ?= cod.$(FORMAT)
WORKERS ?= $(shell $(PYTHON) -c 'import os; print(os.cpu_count() or 1)')
PROGRESS_EVERY ?= 1000

.PHONY: build-cod format-check test check

build-cod:
	PYTHONPATH="src/build-cod$${PYTHONPATH:+:$${PYTHONPATH}}" COD_PATH="$(COD_PATH)" $(PYTHON) -m build_cod --format "$(FORMAT)" --output "$(OUTPUT)" --workers "$(WORKERS)" --progress-every "$(PROGRESS_EVERY)"

format-check:
	$(PYTHON) -m ruff check src/build-cod tests
	$(PYTHON) -m ruff format --check src/build-cod tests

test:
	$(PYTHON) -m pytest

check: format-check test
