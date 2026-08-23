PYTHON ?= python3
COD_PATH ?= ../DATA/COD
FORMAT ?= duckdb
OUTPUT ?= database/cod.$(FORMAT)
WORKERS ?= $(shell $(PYTHON) -c 'import os; print(os.cpu_count()//2 or 1)')
PROGRESS_EVERY ?= 1000
FILTER ?= 1
HOST ?= 127.0.0.1
PORT ?= 8080

.PHONY: install build canonicalize serve format-check test check

ifeq ($(FILTER),0)
FILTER_ARGS = --no-filter
endif

install:
	python3 -m pip install -e .

build:
	mkdir -p database
	PYTHONPATH="src$${PYTHONPATH:+:$${PYTHONPATH}}" COD_PATH="$(COD_PATH)" httk memguard --max-rss-gb 24 --as-gb 12 $(PYTHON) -m build_cod --format "$(FORMAT)" --output "$(OUTPUT)" --workers "$(WORKERS)" --progress-every "$(PROGRESS_EVERY)" $(FILTER_ARGS)
	$(MAKE) canonicalize

canonicalize:
	PYTHONPATH="src$${PYTHONPATH:+:$${PYTHONPATH}}" $(PYTHON) -m build_cod.canonicalize "$(OUTPUT)" --format "$(FORMAT)" --workers "$(WORKERS)" --progress-every "$(PROGRESS_EVERY)" --stats

serve:
	PYTHONPATH="src$${PYTHONPATH:+:$${PYTHONPATH}}" $(PYTHON) -m serve_cod_optimade --format "$(FORMAT)" --database "$(OUTPUT)" --host "$(HOST)" --port "$(PORT)"

format-check:
	$(PYTHON) -m ruff check src tests
	$(PYTHON) -m ruff format --check src tests

test:
	$(PYTHON) -m pytest

check: format-check test
