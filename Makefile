PYTHON ?= python3
COD_PATH ?= ../DATA/COD
FORMAT ?= sqlite
OUTPUT ?= database/cod.$(FORMAT)
WORKERS ?= $(shell $(PYTHON) -c 'import os; print(os.cpu_count() or 1)')
PROGRESS_EVERY ?= 1000
HOST ?= 127.0.0.1
PORT ?= 8080

.PHONY: install build serve format-check test check

install:
	python3 -m pip install -e .

build:
	mkdir -p database
	PYTHONPATH="src$${PYTHONPATH:+:$${PYTHONPATH}}" COD_PATH="$(COD_PATH)" $(PYTHON) -m build_cod --format "$(FORMAT)" --output "$(OUTPUT)" --workers "$(WORKERS)" --progress-every "$(PROGRESS_EVERY)"

serve:
	PYTHONPATH="src$${PYTHONPATH:+:$${PYTHONPATH}}" $(PYTHON) -m serve_cod_optimade --format "$(FORMAT)" --database "$(OUTPUT)" --host "$(HOST)" --port "$(PORT)"

format-check:
	$(PYTHON) -m ruff check src tests
	$(PYTHON) -m ruff format --check src tests

test:
	$(PYTHON) -m pytest

check: format-check test
