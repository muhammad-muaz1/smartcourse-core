# SmartCourse Core

Backend for **SmartCourse**, EduCorp's large-scale learning platform: course
management, publishing, enrollments, progress and analytics for universities,
enterprises and training academies. This repository is the platform backend — a
FastAPI service backed by PostgreSQL (system of record), MongoDB (open-shaped/derived
data) and Redis (cache, locks, rate limits — nothing durable).

Full business context: [`.claude/REQUIREMENT.md`](.claude/REQUIREMENT.md).
API surface and rollout plan: `docs/architecture/api-design.md`.
Architecture rules every change in this repo follows: [`.claude/CLAUDE.md`](.claude/CLAUDE.md)
— read that before touching code; this README is the "how to run it" companion, not a
restatement of the architecture.

## Architecture, in one paragraph

Modelled on NestJS: one flat file per cross-cutting concern in `src/core/` and
`src/db/`, one folder per feature in `src/modules/`, and a routing layer
(`src/core/router.py`) that auto-discovers every module's `routes.py` — adding a module
means creating a folder, never editing a central list. Nothing is scaffolded before it
has a real caller: Kafka/Celery/Temporal folders don't exist yet because nothing
produces or consumes an event yet.

## What's built so far

- **Foundation**: config, structured JSON logging with request-id propagation, the one
  request-context middleware + CORS, the `{"data","meta"}` success envelope, the
  `{"code","message","details","request_id"}` error shape, Postgres/Mongo/Redis
  connection modules, Alembic migrations.
- **`GET /health`** — overall status + per-dependency breakdown, `200`/`503`.
  **`GET /version`** — git SHA + build time, no dependency access.
- **`auth` module** — full lifecycle: register, login, refresh (rotating, with reuse
  detection that revokes the whole token family), logout (revokes the family *and*
  denylists the presented access token), `GET /auth/me`, forgot/reset password, and
  `/.well-known/jwks.json` for offline verification. Argon2id password hashing, RS256
  JWTs. Login and registration are throttled per-IP/per-account via Redis.
- **`users` module** — the `User` model and the lookup/creation functions `auth` calls;
  no user-facing endpoints yet (list/detail/update/role-change are still to build — see
  `docs/architecture/api-design.md` §4).

Everything else in the API design (courses, enrollments, progress, notifications,
analytics) isn't built yet.

## Prerequisites

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)
- A reachable PostgreSQL 16+ and Redis 7+ (locally installed or however you prefer —
  this repo doesn't yet ship a `deploy/docker-compose.yml`; see "What's not built yet"
  in `.claude/CLAUDE.md` §3). MongoDB 7+ if you want `/health` fully green, though
  nothing currently persists anything to it.

## Getting started

```bash
git clone <repo> && cd smartcourse-core

uv sync --extra dev          # installs the app + dev tooling into .venv

cp env.example .env          # then edit the values below to match your local setup
```

Edit `.env` for your local Postgres/Mongo/Redis:

```bash
SMARTCOURSE_POSTGRES__DSN=postgresql+asyncpg://<user>:<password>@localhost:5432/<db>
SMARTCOURSE_MONGO__URL=mongodb://localhost:27017
SMARTCOURSE_MONGO__DATABASE=<db>
SMARTCOURSE_REDIS__URL=redis://localhost:6379/0
```

The Postgres database itself must already exist (`createdb <db>`) — migrations create
tables, not the database.

```bash
make keys      # generates a dev JWT signing keypair into secrets/ (gitignored)
make migrate   # alembic upgrade head — creates the users/refresh_tokens tables
make dev       # runs the API with auto-reload, on SMARTCOURSE_APP__HOST/PORT from .env
```

Verify it's up:

```bash
curl http://localhost:8000/api/v1/health | python3 -m json.tool
```

Each dependency shows its own status — `postgres`/`mongo`/`redis` all `"ok"` means
you're fully set up; anything else names exactly which one isn't reachable and why.

### Try the auth flow

```bash
# Register (student or instructor only — admin is never self-assignable)
curl -X POST localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"correcthorse123","full_name":"You","role":"student"}'

# Log in
curl -X POST localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"correcthorse123"}'
# -> {"data": {"access_token": "...", "refresh_token": "...", ...}}

# Use the access token
curl localhost:8000/api/v1/auth/me -H "Authorization: Bearer <access_token>"
```

Interactive API docs: `http://localhost:8000/docs`.

## Project layout

```
src/
├── main.py, app.py, config.py    # entrypoint, app assembly, all settings
├── core/                         # cross-cutting: router, response, exceptions,
│                                  # handlers, middleware, logging, security, dependencies
├── db/                           # connections only: base, postgres, mongo, redis
└── modules/
    ├── health/                   # GET /health, GET /version
    ├── auth/                     # register/login/refresh/logout/me/password reset
    └── users/                    # User model + lookups (no routes yet)

migrations/postgres/   # Alembic
tests/                 # mirrors src/modules/, plus tests/core/ and tests/db/
docs/adr/               # decisions worth knowing the "why" behind
```

## Commands

```bash
make dev        # API with auto-reload (host/port/reload from .env)
make migrate    # alembic upgrade head
make keys       # generate a dev JWT signing keypair (safe to re-run, never overwrites)
make test       # full test suite (unit + integration — integration needs Docker)
make lint       # ruff check + format --check
make types      # mypy --strict on src/
make check      # lint + types + test — run before every commit
make shell      # python shell with the app already constructed
```

`make up`/`down`/`logs`/`seed` are declared but not backed yet — they fail with a clear
"no such file" until `deploy/docker-compose.yml` and a seed script exist.

## Testing

Unit tests touch no I/O. Integration tests use
[testcontainers](https://testcontainers.com/) against real Postgres/Mongo/Redis — never
a mocked database (see `.claude/CLAUDE.md` §12) — so `make test` needs Docker for the
full suite to run; without it, `pytest tests/core tests/health tests/db/test_base.py`
still exercises everything that doesn't require a container.

## Decisions worth reading before changing core infrastructure

`docs/adr/` has the full list; the ones most likely to bite you if you don't know them:

- **0003** — Starlette's middleware stack is LIFO, not FIFO. Middleware is registered
  in `app.py` in the *reverse* of its execution order.
- **0005** / **0008** — PyMongo, asyncpg and SQLAlchemy's `Enum`/`DateTime` columns all
  have a default that silently does the wrong thing here (30s hangs on an unreachable
  Mongo, naive timestamp columns rejecting timezone-aware datetimes, enum columns
  storing the Python member's name instead of its value). Fixed, but worth knowing the
  defaults are traps if you add a similar column or client elsewhere.
- **0008** — why `auth` is allowed to import `users`' model and service directly, and
  why the reuse-detection code path in `auth/service.py` explicitly commits before
  raising, against the usual "services don't commit" rule.
