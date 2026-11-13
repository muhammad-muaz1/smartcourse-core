.PHONY: install env keys db migrate setup cutover dev worker celery temporal kafka jaeger prometheus create-admin backfill shell test test-local smoke lint types check up down logs

# Deployables that own a database, and everything that runs as a process.
SERVICES := identity course learning notification analytics
APPS := $(SERVICES) gateway
# Services that also run a worker process (python -m src.worker): outbox relay, Kafka
# consumers and/or a Temporal worker.
WORKERS := identity course learning notification analytics

ROOT := $(CURDIR)
VENV := $(ROOT)/.venv/bin
UV ?= uv

# services/<name>, except the gateway, which sits beside services/ (it's infrastructure).
dir_of = $(if $(filter gateway,$(1)),gateway,services/$(1))
# `make migrate s=course` targets one; no `s` means all of them.
selected = $(if $(s),$(s),$(1))

# --- setup -------------------------------------------------------------------------

install:
	$(UV) sync --all-packages

# Per-service .env from its env.example. An existing .env is never overwritten: only
# keys it lacks are appended (new settings arrive without touching local values).
env:
	$(VENV)/python -m scripts.sync_env $(APPS)

# Dev-only JWT signing keypair, generated once into the repo-root secrets/ (gitignored).
# identity signs with the private key; every other service gets only the public key.
keys:
	cd services/identity && $(VENV)/python -m scripts.generate_dev_keys

# Creates every service's database if missing. Migrations create tables, not databases.
db:
	$(VENV)/python -m scripts.create_databases

migrate:
	@for svc in $(call selected,$(SERVICES)); do \
		echo "== $$svc"; (cd services/$$svc && $(VENV)/alembic upgrade head) || exit 1; \
	done

# Fresh machine: everything needed before `make dev`.
setup: env keys db migrate

# One-off, for a machine that ran the monolith: per-service .env files from the
# repo-root .env, the new databases, then the legacy rows copied across. The legacy
# database is only read from. Safe to re-run.
cutover:
	$(VENV)/python -m scripts.cutover_from_monolith env
	$(MAKE) env db migrate
	$(VENV)/python -m scripts.cutover_from_monolith data

# --- run ---------------------------------------------------------------------------

# `make dev` runs every service, the gateway, every worker and the Celery worker, the
# APIs with auto-reload; Ctrl+C stops them all. `make dev s=course` runs just one API.
# Host/port/reload come from each one's .env. Workers need Kafka (`make kafka`) and the
# course worker Temporal (`make temporal`); traces go to Jaeger (`make jaeger`).
dev:
ifdef s
	cd $(call dir_of,$(s)) && $(VENV)/python -m src.main
else
	@trap 'kill 0' INT TERM; \
	for app in $(APPS); do \
		dir=$$( [ $$app = gateway ] && echo gateway || echo services/$$app ); \
		(cd $$dir && exec $(VENV)/python -m src.main) & \
	done; \
	for svc in $(WORKERS); do \
		(cd services/$$svc && exec $(VENV)/python -m src.worker) & \
	done; \
	$(MAKE) --no-print-directory celery & \
	wait
endif

# One worker process — the outbox relay plus Kafka consumers and/or a Temporal worker,
# whatever that service runs. No reload: restart it after changing a consumer,
# workflow or activity. `make worker s=course`.
worker:
	cd services/$(s) && $(VENV)/python -m src.worker

# notification's Celery worker with embedded beat (the stale-delivery sweep), on both of
# its queues. Prefork children write metrics into a fresh multiprocess directory per
# run, which the parent serves on celery.metrics_port.
celery:
	@mkdir -p .tools
	cd services/notification && PROMETHEUS_MULTIPROC_DIR=$$(mktemp -d) \
		$(VENV)/celery -A src.celery_app worker --beat --loglevel=INFO --concurrency=2 \
		--queues=notifications,maintenance --schedule=$(ROOT)/.tools/celerybeat-schedule

# Local Temporal dev server (gRPC :7233, Web UI http://localhost:8233), state kept in
# .temporal/ so runs survive a restart. Needs the `temporal` CLI on PATH:
# https://docs.temporal.io/cli#install
temporal:
	@mkdir -p .temporal
	temporal server start-dev --db-filename .temporal/dev.db

# Local infrastructure without Docker, each in its own terminal: official release
# binaries downloaded once into .tools/ (checksum-verified), state kept there too.
# kafka: Kafka 4 (KRaft, single node) on :9092 — needs Java 17+.
kafka:
	scripts/dev_infra.sh kafka

# jaeger: traces — UI http://localhost:16686, OTLP :4317/:4318.
jaeger:
	scripts/dev_infra.sh jaeger

# prometheus: http://localhost:9090, scraping every API and worker.
prometheus:
	scripts/dev_infra.sh prometheus

# The only way to get an admin account — self-registration can't grant that role.
# Prompts for the password interactively; see services/identity/scripts/create_admin.py.
create-admin:
	cd services/identity && $(VENV)/python -m scripts.create_admin --email $(EMAIL) --full-name "$(NAME)"

# One-off, after upgrading a machine that has data from before week 3: re-announce every
# existing user, course and enrollment through the outboxes, so learning, notification
# and analytics hear about them. Needs the workers running to deliver. Safe to repeat
# (consumers upsert), but each run re-announces everything — run it once.
backfill:
	cd services/identity && $(VENV)/python -m scripts.backfill_events
	cd services/course && $(VENV)/python -m scripts.backfill_events
	@echo "waiting for course_cache to take the new snapshots before enrollments..." && sleep 5
	cd services/learning && $(VENV)/python -m scripts.backfill_events

shell:
	cd $(call dir_of,$(if $(s),$(s),identity)) && $(VENV)/python -i -c "from src.app import create_app; app = create_app(); print('app created - see the app variable')"

# --- quality -----------------------------------------------------------------------

# Integration tests use testcontainers and need Docker; unit tests don't.
test:
	cd libs/platform-core && $(VENV)/pytest
	cd libs/contracts && $(VENV)/pytest
	@for app in $(call selected,$(APPS)); do \
		echo "== $$app"; (cd $(ROOT)/$$( [ $$app = gateway ] && echo gateway || echo services/$$app ) && $(VENV)/pytest) || exit 1; \
	done

# The same suites without Docker: a throwaway Postgres cluster and Redis under .tools/
# (scripts/test_infra.sh), stopped afterwards whether the tests pass or not.
test-local:
	@eval "$$(scripts/test_infra.sh start)"; \
	$(MAKE) --no-print-directory test s=$(s); status=$$?; \
	scripts/test_infra.sh stop; exit $$status

# End-to-end flows against the running stack (`make dev` + kafka + temporal), through
# the gateway. Creates throwaway users and courses (...@example.com).
smoke:
	$(VENV)/python -m scripts.e2e_smoke

lint:
	$(VENV)/ruff check libs services gateway scripts
	$(VENV)/ruff format --check libs services gateway scripts

# Each member is type-checked from its own directory, so `src` means that member only.
types:
	cd libs/platform-core && $(VENV)/mypy --config-file $(ROOT)/pyproject.toml -p platform_core
	cd services/identity && $(VENV)/mypy --config-file $(ROOT)/pyproject.toml src scripts
	cd libs/contracts && $(VENV)/mypy --config-file $(ROOT)/pyproject.toml -p smartcourse_contracts
	cd services/course && $(VENV)/mypy --config-file $(ROOT)/pyproject.toml src scripts
	cd services/learning && $(VENV)/mypy --config-file $(ROOT)/pyproject.toml src scripts
	cd services/notification && $(VENV)/mypy --config-file $(ROOT)/pyproject.toml src
	cd services/analytics && $(VENV)/mypy --config-file $(ROOT)/pyproject.toml src
	cd gateway && $(VENV)/mypy --config-file $(ROOT)/pyproject.toml src
	$(VENV)/mypy --config-file $(ROOT)/pyproject.toml scripts

check: lint types test

# --- containers --------------------------------------------------------------------

# Everything in Docker: Postgres, Redis, Temporal, every service, its worker and the
# gateway. `s=course`
# starts one service plus what it depends on.
up:
	docker compose -f deploy/docker-compose.yml up -d --build $(s)

down:
	docker compose -f deploy/docker-compose.yml down

logs:
	docker compose -f deploy/docker-compose.yml logs -f $(s)
