.PHONY: setup lint format test test-all ingest ingest-zones verify-raw pipeline pipeline-smoke reproduce compose-up compose-down mlflow-ui

setup:        ## Install the locked environment, including dev and train (dvc, mlflow) tools
	uv sync --frozen --group train

lint:         ## Static checks: ruff lint, ruff format check, mypy on src/
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

format:       ## Rewrite files to ruff's format (only target that mutates code)
	uv run ruff format .

test:         ## Fast tests only (what CI runs)
	uv run pytest -m "not slow"

test-all:     ## Every test including slow ones
	uv run pytest

ingest:       ## Fetch month(s) byte-for-byte: make ingest MONTH="2024-10 2024-11"
	uv run python scripts/ingest.py --service yellow --month $(MONTH)

ingest-zones: ## Fetch the TLC zone lookup CSV
	uv run python scripts/ingest.py --zones

verify-raw:   ## Recompute md5 of every raw file and compare with its ingest report
	uv run python scripts/ingest.py --verify

pipeline:     ## Run every DVC stage whose inputs changed (validate -> prepare -> train -> evaluate)
	uv run dvc repro

pipeline-smoke: ## Whole pipeline on tests/fixtures in a temp dir with SQLite MLflow (what CI runs)
	uv run pytest tests/test_pipeline.py -q

reproduce:    ## Exit criterion 1: fresh clone -> dvc pull -> dvc repro -> metrics identical
	scripts/reproduce.sh

compose-up:   ## Start MLflow tracking server + Postgres (http://localhost:5001)
	docker compose up -d --build --wait

compose-down: ## Stop the stack (volumes kept; add -v to wipe)
	docker compose down

mlflow-ui:    ## Open the MLflow UI
	open http://localhost:5001
