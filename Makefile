.PHONY: setup lint-infra audit lint format test test-all ingest ingest-zones verify-raw quality pipeline pipeline-smoke reproduce compose-up compose-api compose-down mlflow-ui register promote promote-recover promote-abort cost registry-backup registry-restore-check rollback candidate-ci registry-status serve docker-build docker-build-champion docker-run lambda-drill train-env

setup:        ## Install the locked environment, including dev and train (dvc, mlflow) tools
	uv sync --frozen --group train

SHELLCHECK_IMAGE := koalaman/shellcheck@sha256:61862eba1fcf09a484ebcc6feea46f1782532571a34ed51fedf90dd25f925a8d
ACTIONLINT_IMAGE := rhysd/actionlint@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667

lint-infra:   ## shellcheck every script, actionlint every workflow (pinned images; CI runs this)
	docker run --rm -v "$$PWD:/mnt" -w /mnt $(SHELLCHECK_IMAGE) --version | sed -n 2p
	docker run --rm -v "$$PWD:/mnt" -w /mnt $(SHELLCHECK_IMAGE) deploy/aws/*.sh scripts/*.sh
	docker run --rm -v "$$PWD:/repo" -w /repo $(ACTIONLINT_IMAGE) -no-color

audit:        ## Dependency vulnerabilities from uv.lock: runtime strict, other groups vs security/pip-audit-ignore.txt
	scripts/audit.sh

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

quality:      ## Data-quality report + acceptance rules (exits 1 if a month is unfit)
	uv run python -m tripduration.quality

pipeline:     ## Run every stale DVC stage in the canonical training env (linux/amd64, serving base image)
	scripts/train_env.sh uv run --locked dvc repro

train-env:    ## Build the canonical training image (Dockerfile target `train`) and print its id
	scripts/train_env.sh python -c "import platform, sys; print(platform.platform(), sys.version.split()[0])"

pipeline-smoke: ## Whole pipeline on tests/fixtures in a temp dir with SQLite MLflow (what CI runs)
	uv run pytest tests/test_pipeline.py -q

reproduce:    ## Exit criterion 1: clone REV (default HEAD) -> inputs (SOURCE=remote|public) -> repro in train env -> bit-identical predictions
	REV="$(REV)" SOURCE="$(or $(SOURCE),remote)" scripts/reproduce.sh

compose-up:   ## Start MLflow tracking server + Postgres (http://localhost:5001); needs no model files
	docker compose up -d --build --wait postgres mlflow

compose-api:  ## Build and start the API from local models/ (after dvc pull or make pipeline): http://localhost:8080
	docker compose up -d --build --wait api

compose-down: ## Stop the stack (volumes kept; add -v to wipe)
	docker compose down

mlflow-ui:    ## Open the MLflow UI
	open http://localhost:5001

register:     ## Register current DVC outputs as a new model version (refuses dirty git / stale dvc)
	uv run python scripts/register.py

promote:      ## Promote a version: make promote VERSION=2 REASON="beats v1 on 2024-12"
	uv run python scripts/promote.py --version $(VERSION) --reason "$(REASON)"

cost:         ## Plan, credits and month-to-date cost by service (usage / credits / net): make cost [MONTH=YYYY-MM]
	uv run python scripts/cost_report.py $(if $(MONTH),--month $(MONTH),)

registry-backup: ## Dump the local registry (Postgres + artifacts + manifest) to backups/ and S3
	uv run python scripts/registry_backup.py --upload s3://$${S3_BUCKET:-nyc-taxi-trip-duration-560512681455}/backups/registry

registry-restore-check: ## Restore FROM=<dir|s3://...> into a scratch project and verify it: make registry-restore-check FROM=...
	uv run python scripts/registry_restore.py --from "$(FROM)" --check

promote-recover: ## Finish an interrupted promote/rollback/refresh (see docs/runbook.md)
	uv run python scripts/promote.py --recover

promote-abort: ## Undo an interrupted promote/rollback/refresh
	uv run python scripts/promote.py --abort

rollback:     ## Roll champion back to previous_version: make rollback REASON="deploy_check failed"
	uv run python scripts/promote.py --rollback --reason "$(REASON)"

candidate-ci: ## Approve a candidate PR's held CI run, wait, assert required checks + CLEAN: make candidate-ci BRANCH=retrain/2025-04
	scripts/candidate_ci.sh $(BRANCH) --approve

registry-status: ## Aliases, versions and champion.json side by side
	uv run python scripts/promote.py --status

serve:        ## Run the API locally from the working tree (models/, data/reference/)
	uv run uvicorn tripduration.api.main:app --port 8080 --no-access-log

docker-build: ## Build the serving image from local models/ (stamps git sha)
	docker build --build-arg GIT_SHA=$$(git rev-parse HEAD) -t tripduration:dev .

docker-build-champion: ## Fetch champion artefacts from the DVC remote and build the deploy image
	uv run python scripts/fetch_champion.py
	docker build --build-arg MODELS_SRC=build/champion/models --build-arg REFERENCE_SRC=build/champion/reference \
	  --build-arg GIT_SHA=$$(git rev-parse HEAD) \
	  --build-arg MODEL_VERSION=v$$(uv run python -c "import json;print(json.load(open('models/champion.json'))['version'])") \
	  -t tripduration:champion .

lambda-drill: ## Prove lambda.sh creates AND reconciles, on a scratch function it deletes: make lambda-drill IMAGE=<ecr uri@sha256:...>
	deploy/aws/drill_lambda.sh $(IMAGE)

docker-run:   ## Run the image on :8080
	docker run --rm -p 8080:8080 tripduration:dev
