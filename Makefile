PYTHON ?= $(or $(wildcard ../.venv/bin/python),python3)
ICSD_PATH ?= ../DATA/ICSD/2026.1/cif-experimental
FORMAT ?= duckdb
IMPORT_OUTPUT ?= database/icsd.$(FORMAT)
CANONICAL_OUTPUT ?= database/icsd-canonical.$(FORMAT)
DISTINCT_OUTPUT ?= database/icsd-distinct.$(FORMAT)
WORKERS ?= $(shell $(PYTHON) -c 'import os; print(os.cpu_count()//2 or 1)')
PROGRESS_EVERY ?= 1000
COMMIT_EVERY ?= 200
CANONICAL_MAX_ASU_SITES ?= 64
DISTINCT_DELTA ?= 0.1
DISTINCT_GRID_DIMENSIONS ?= 2
DISTINCT_GRID_STRATEGY ?= variance
DISTINCT_MAX_COVERAGE_SIZE ?= 150
DISTINCT_PROGRESS_EVERY ?= 200
DISTINCT_INGEST_CHUNK ?= 5000
DISTINCT_COMMIT_EVERY ?= 5000
DISTINCT_WORKER_MEMORY_LIMIT ?= 512MB
DISTINCT_WORKER_MAX_TASKS ?= 200
HTTK_DUCKDB_MEMORY_LIMIT ?= 6GB
export HTTK_DUCKDB_MEMORY_LIMIT
HOST ?= 127.0.0.1
PORT ?= 8080

.PHONY: install build canonicalize distinct serve format-check test check ci

install:
	$(PYTHON) -m pip install --no-deps --no-build-isolation -e .

build:
	mkdir -p database
	PYTHONPATH="src$${PYTHONPATH:+:$${PYTHONPATH}}" ICSD_PATH="$(ICSD_PATH)" $(PYTHON) -c 'from httk.core.cli import main; raise SystemExit(main())' memguard --max-rss-gb 24 $(PYTHON) -m build_icsd --format "$(FORMAT)" --output "$(IMPORT_OUTPUT)" --workers "$(WORKERS)" --progress-every "$(PROGRESS_EVERY)" --commit-every "$(COMMIT_EVERY)"

canonicalize:
	PYTHONPATH="src$${PYTHONPATH:+:$${PYTHONPATH}}" $(PYTHON) -c 'from httk.core.cli import main; raise SystemExit(main())' memguard --max-rss-gb 24 $(PYTHON) -m build_icsd.canonicalize "$(IMPORT_OUTPUT)" --output "$(CANONICAL_OUTPUT)" --format "$(FORMAT)" --workers "$(WORKERS)" --progress-every "$(PROGRESS_EVERY)" --chunk "$(COMMIT_EVERY)" --max-asu-sites "$(CANONICAL_MAX_ASU_SITES)" --stats

distinct:
	PYTHONPATH="src$${PYTHONPATH:+:$${PYTHONPATH}}" $(PYTHON) -c 'from httk.core.cli import main; raise SystemExit(main())' memguard --max-rss-gb 24 $(PYTHON) -m build_icsd.distinct "$(CANONICAL_OUTPUT)" --output "$(DISTINCT_OUTPUT)" --format "$(FORMAT)" --workers "$(WORKERS)" --progress-every "$(DISTINCT_PROGRESS_EVERY)" --ingest-chunk "$(DISTINCT_INGEST_CHUNK)" --commit-every "$(DISTINCT_COMMIT_EVERY)" --worker-memory-limit "$(DISTINCT_WORKER_MEMORY_LIMIT)" --worker-max-tasks "$(DISTINCT_WORKER_MAX_TASKS)" --delta "$(DISTINCT_DELTA)" --grid-dimensions "$(DISTINCT_GRID_DIMENSIONS)" --grid-strategy "$(DISTINCT_GRID_STRATEGY)" --max-coverage-size "$(DISTINCT_MAX_COVERAGE_SIZE)" --stats

serve:
	PYTHONPATH="src$${PYTHONPATH:+:$${PYTHONPATH}}" $(PYTHON) -m serve_icsd_optimade --format "$(FORMAT)" --database "$(CANONICAL_OUTPUT)" --host "$(HOST)" --port "$(PORT)"

format-check:
	$(PYTHON) -m ruff check src tests
	$(PYTHON) -m ruff format --check src tests

test:
	$(PYTHON) -m pytest

check: format-check test

ci: check
