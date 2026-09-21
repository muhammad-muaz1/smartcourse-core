.PHONY: dev up down logs migrate seed test lint types check shell keys

VENV := .venv/bin

# --- Working today
# Host/port/reload come from .env (SMARTCOURSE_APP__HOST/PORT/RELOAD) via src/main.py,
# not a hardcoded CLI flag here — one source of truth instead of two.
dev:
	$(VENV)/python -m src.main

migrate:
	$(VENV)/alembic upgrade head

# Dev-only JWT signing keypair, generated once into secrets/ (gitignored). Safe to
# re-run — it never overwrites an existing keypair.
keys:
	$(VENV)/python -m scripts.generate_dev_keys

test:
	$(VENV)/pytest

lint:
	$(VENV)/ruff check src/ tests/
	$(VENV)/ruff format --check src/ tests/

types:
	$(VENV)/mypy src/

check: lint types test

shell:
	$(VENV)/python -i -c "from src.app import create_app; app = create_app(); print('app created - see the app variable')"

# --- Deploy targets (deploy/ compose files don't exist yet — these fail with a clear
# "no such file" until then, which is correct: a target that pretends to work before
# its backing files exist would hide that gap).
up:
	docker compose -f deploy/docker-compose.yml up -d

down:
	docker compose -f deploy/docker-compose.yml down

logs:
	docker compose -f deploy/docker-compose.yml logs -f $(s)

# --- Lands once there's business data worth seeding
seed:
	$(VENV)/python -m scripts.seed
