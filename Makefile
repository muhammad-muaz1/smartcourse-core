.PHONY: install env keys db migrate setup cutover dev worker temporal create-admin shell test lint types check up down logs

# Deployables that own a database, and everything that runs as a process.
SERVICES := identity course learning
APPS := $(SERVICES) gateway
# Services that also run a Temporal worker process (python -m src.worker).
WORKERS := course learning

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

# Per-service .env from its env.example — never overwrites an existing .env.
env:
	@for app in $(APPS); do \
		dir=$$( [ $$app = gateway ] && echo gateway || echo services/$$app ); \
		if [ -f $$dir/.env ]; then echo "$$dir/.env exists — leaving it alone"; \
		else cp $$dir/env.example $$dir/.env && echo "created $$dir/.env"; fi; \
	done

# Dev-only JWT signing keypair, generated once into the repo-root secrets/ (gitignored).
# identity signs with the private key; every other service gets only the public key.
keys:
	cd services/identity && $(VENV)/python -m scripts.generate_dev_keys

# Creates identity_db, course_db and learning_db if missing. Migrations create tables, not databases.
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

# `make dev` runs every service, the gateway and every worker, the APIs with
# auto-reload; Ctrl+C stops them all. `make dev s=course` runs just one API.
# Host/port/reload come from each one's .env. Workers need Temporal: `make temporal`.
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
	wait
endif

# One worker process (Temporal worker; for course also the outbox relay). No reload:
# restart it after changing a workflow or activity. `make worker s=course`.
worker:
	cd services/$(s) && $(VENV)/python -m src.worker

# Local Temporal dev server (gRPC :7233, Web UI http://localhost:8233), state kept in
# .temporal/ so runs survive a restart. Needs the `temporal` CLI on PATH:
# https://docs.temporal.io/cli#install
temporal:
	@mkdir -p .temporal
	temporal server start-dev --db-filename .temporal/dev.db

# The only way to get an admin account — self-registration can't grant that role.
# Prompts for the password interactively; see services/identity/scripts/create_admin.py.
create-admin:
	cd services/identity && $(VENV)/python -m scripts.create_admin --email $(EMAIL) --full-name "$(NAME)"

shell:
	cd $(call dir_of,$(if $(s),$(s),identity)) && $(VENV)/python -i -c "from src.app import create_app; app = create_app(); print('app created - see the app variable')"

# --- quality -----------------------------------------------------------------------

# Integration tests use testcontainers and need Docker; unit tests don't.
test:
	cd libs/platform-core && $(VENV)/pytest
	@for app in $(call selected,$(APPS)); do \
		echo "== $$app"; (cd $(ROOT)/$$( [ $$app = gateway ] && echo gateway || echo services/$$app ) && $(VENV)/pytest) || exit 1; \
	done

lint:
	$(VENV)/ruff check libs services gateway scripts
	$(VENV)/ruff format --check libs services gateway scripts

# Each member is type-checked from its own directory, so `src` means that member only.
types:
	cd libs/platform-core && $(VENV)/mypy --config-file $(ROOT)/pyproject.toml -p platform_core
	cd services/identity && $(VENV)/mypy --config-file $(ROOT)/pyproject.toml src scripts
	cd libs/contracts && $(VENV)/mypy --config-file $(ROOT)/pyproject.toml -p smartcourse_contracts
	cd services/course && $(VENV)/mypy --config-file $(ROOT)/pyproject.toml src
	cd services/learning && $(VENV)/mypy --config-file $(ROOT)/pyproject.toml src
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
