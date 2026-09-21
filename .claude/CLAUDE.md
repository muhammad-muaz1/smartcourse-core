# CLAUDE.md — SmartCourse Core

Standing context for every session in this repository. Read this before writing code.

---

## 1. What this project is

The backend for SmartCourse, a large-scale learning platform: course management, publishing,
enrollments, progress, notifications and analytics.

The business problems it exists to solve:

- Content publishing is slow and manual.
- Course data, progress and analytics are scattered and inconsistent.
- High traffic delays enrollments, notifications and background tasks.
- Background workflows are unreliable and failures are hard to diagnose.

Consistency, throughput, recoverability and observability are the product. CRUD is the easy part.

An AI layer (Q&A, semantic search, content generation) will be a **separate service** later.
Never add LLM SDKs, embeddings or a vector database here.

---

## 2. Architecture philosophy

Modelled on **NestJS**: one folder per feature module, and inside it a small, predictable set of
flat files. A developer opening any module sees the same filenames and knows immediately where
everything lives.

| NestJS | Here |
| --- | --- |
| `AppModule` | `src/app.py` |
| Module folder | `src/modules/<name>/` |
| Controller | `routes.py` |
| Service / provider | `service.py` |
| Entity | `models.py` |
| DTO | `schemas.py` |
| Global filters, interceptors, guards | `src/core/` |

**Three rules that keep this from scattering as it grows:**

1. **A file exists only when something imports it.** No placeholder files, no "we'll need this
   later" modules. If you cannot name what a file does in one line, it should not exist.
2. **Grow inside a module before growing the tree.** A module stays flat files until one file
   passes ~300 lines; only then does that *one* file become a folder. Never pre-split.
3. **Shared infrastructure moves to `core/` on the second use, not the first.** The first time
   something is needed, it lives in the module that needs it.

---

## 3. Folder structure

```
src/
├── main.py                 # entrypoint: uvicorn runner only
├── app.py                  # create_app() — assembles the whole application
├── config.py               # all settings, one file, pydantic-settings
│
├── core/                   # cross-cutting, no business knowledge
│   ├── router.py           # auto-discovers every module's routes.py
│   ├── response.py         # success envelope + pagination helpers
│   ├── exceptions.py       # AppError and its subclasses
│   ├── handlers.py         # exception → HTTP response, registered once
│   ├── middleware.py       # every middleware we actually use, one file
│   ├── logging.py          # structlog setup + request context
│   ├── security.py         # password hashing + JWT encode/decode
│   └── dependencies.py     # current_user, require_roles, pagination params
│
├── db/                     # connections only — no queries, no models
│   ├── base.py             # SQLAlchemy Base + shared column mixins
│   ├── postgres.py         # engine, session factory, get_session, check()
│   ├── mongo.py            # client, index init, get_mongo, check()
│   └── redis.py            # client, get_redis, check()
│
└── modules/                # one folder per feature
    ├── health/
    ├── auth/
    ├── users/
    ├── courses/
    ├── enrollments/
    ├── progress/
    ├── notifications/
    └── analytics/

migrations/      alembic/ (postgres) + mongo/ (index + data scripts)
tests/           mirrors src/modules/ one-to-one
deploy/          Dockerfile, compose files, observability configs
docs/            adr/, prd/, runbooks/
```

Imports are absolute from the repo root: `from src.core.response import success`.

Folders **not** created until the feature that needs them is being built: `src/events/` (Kafka
producer, consumer, outbox), `src/workers/` (Celery), `src/workflows/` (Temporal). They are
planned, not scaffolded. An empty folder is a place for confusion to accumulate.

### What each `db/` file does — and nothing more

| File | Responsibility |
| --- | --- |
| `base.py` | `Base`, `UUIDMixin`, `TimestampMixin`. Nothing else. |
| `postgres.py` | Async engine, session factory, `get_session` dependency, `check()` for health. |
| `mongo.py` | Client, index registration at startup, `get_mongo` dependency, `check()`. |
| `redis.py` | Client, `get_redis` dependency, `check()`. |

No repositories, no queries and no models live in `db/`. Those belong to the module that owns
the data.

---

## 4. Module anatomy

Every module has the same shape. Files are created only when needed:

```
modules/courses/
├── routes.py        # HTTP layer: validate, call service, return. Always exists.
├── service.py       # business logic. Exists as soon as there is logic.
├── models.py        # SQLAlchemy models (or Mongo documents)
├── schemas.py       # Pydantic request/response models
└── events.py        # events this module emits — only once events exist
```

Add `repository.py` **only** when a module's queries get complex enough that `service.py` is hard
to read. Until then the service uses the session directly. One data-access class per table from
day one is the kind of ceremony that makes a codebase feel heavy without making it safer.

**Routing contract.** Every `routes.py` exposes exactly one module-level `router`:

```python
router = APIRouter(prefix="/courses", tags=["courses"])
```

`core/router.py` walks `src/modules/*/routes.py`, imports each one, and includes its `router`
under `/api/v1`. Adding a module means creating the folder — never editing a central
registration list. That list is the file everyone forgets to update.

**Layering.** `routes.py → service.py → models.py`, dependencies pointing one way.
`core/` and `db/` never import from `modules/`. Modules never import another module's `service.py`
or `models.py` — cross-module work goes through events, or through a small function the owning
module exposes deliberately.

---

## 5. Health

**One route: `GET /health`.** It returns overall status plus a per-dependency breakdown, `200`
when healthy and `503` when not:

```json
{"status": "ok", "version": "abc123", "services": {
  "postgres": {"status": "ok", "latency_ms": 3},
  "mongo":    {"status": "ok", "latency_ms": 5},
  "redis":    {"status": "ok", "latency_ms": 1}
}}
```

Each dependency exposes a `check()` in its `db/` file; health calls them concurrently with a
per-check timeout and caches the result ~5s. Adding Kafka, RabbitMQ or Temporal later means
adding one entry to that list and nothing else.

`GET /version` stays separate (git SHA, build time) because it needs no dependency access. That
is all. No `/health/live`, `/health/startup` or `/health/db`.

> Revisit at deployment, not now: under Kubernetes the liveness probe must not point at
> `/health`, or a Redis blip restarts healthy pods. Point liveness at `/version`, or split the
> route then — when there is a concrete reason to.

---

## 6. Middleware — only what runs

`core/middleware.py` holds everything, registered in `app.py` in this order:

1. **`RequestContextMiddleware`** — accepts or mints `X-Request-ID`, puts it in a contextvar, and
   logs one line per request with method, path, status and duration.
2. **`CORSMiddleware`** — explicit origins from settings, never `*` outside local.

That is the whole stack today. Rate limiting, idempotency keys, body-size limits, security
headers and trusted hosts are all real needs, and each gets added **when the endpoint that needs
it is built**, with a test that proves it works. Middleware that runs on every request and is
exercised by nothing is worse than none: it costs latency and hides bugs.

Authentication is **not** middleware. It is a dependency (`Depends(get_current_user)`), so
OpenAPI documents which routes need it and unauthenticated routes are explicit rather than
accidental.

---

## 7. Requests and responses

- Versioned prefix `/api/v1`. Plural nouns; verbs live in the HTTP method.
- Success: `{"data": ..., "meta": {...}}` via `core/response.py`. Never a bare dict or list.
- Errors: one shape, built only by `core/handlers.py` —
  `{"code": "COURSE_NOT_FOUND", "message": "...", "details": [...], "request_id": "..."}`.
  No route ever constructs an error body.
- Pagination is cursor-based (`?limit=&cursor=`), with `meta.next_cursor` in the response.
- Long operations return `202` with a status URL. Never block a request on background work.

Exceptions live in `core/exceptions.py`: `AppError` → `NotFoundError`, `ConflictError`,
`ValidationError`, `UnauthorizedError`, `ForbiddenError`, `BusinessRuleError`, each carrying a
stable `code`. HTTP status mapping lives in `handlers.py` — **service code never imports
`fastapi`**.

---

## 8. Data

**PostgreSQL is the system of record:** users, roles, refresh tokens, courses, modules, lessons
(metadata and ordering), enrollments, progress, certificates. Anything with an invariant that
must hold at commit time.

**MongoDB holds open-shaped and derived data:** lesson bodies, content chunks, publish runs,
notifications, analytics projections. All rebuildable.

**Redis holds nothing durable:** cache, rate limits, locks, idempotency responses.

Rules:

- Invariants are database constraints, not application `if` statements. Duplicate enrollment is
  prevented by a unique constraint on `(student_id, course_id)`, not by a `SELECT` first.
- Seat limits use `SELECT … FOR UPDATE` inside the same transaction as the insert.
  Check-then-insert without a lock is a race that will fire under launch traffic.
- No foreign keys across stores. Mongo documents reference Postgres rows by UUID.
- Timezone-aware UTC only: `datetime.now(tz=UTC)`, never `utcnow()`, never a naive datetime.
- One transaction per request, owned by the route's session dependency. Services do not commit;
  they raise or return.

Stack: SQLAlchemy 2.0 async (`asyncpg`) with typed `Mapped[]` models, Alembic for migrations,
Beanie + Pydantic v2 for Mongo, `redis.asyncio`. **Do not use SQLModel** — one class serving as
both table and API schema couples the wire format to storage.

---

## 9. Background work — when it arrives

Not built yet. When it is, one job per tool, in `src/events/`, `src/workers/` and
`src/workflows/` respectively:

| Tool | Use for | Never for |
| --- | --- | --- |
| **Temporal** | Multi-step process that must complete or compensate and survive a restart — course publishing | Anything on the request path |
| **Kafka** | A fact that already happened, broadcast to unknown consumers — the analytics log | Commands to a known recipient |
| **Celery** | One retryable unit of work, seconds not hours — send an email, recompute a counter | Orchestrating multi-step processes |

Temporal orchestrates. Kafka informs. Celery executes.

Two rules to design toward now, because retrofitting them is expensive: a state change that
emits an event writes the event row in the **same transaction** as the change (transactional
outbox), and every consumer dedupes by event id. Do not build either until the first real event
exists — but do not write a path that makes them impossible.

---

## 10. Security

- Passwords: Argon2id (`argon2-cffi` or `pwdlib`; `passlib` is unmaintained).
- JWT **RS256** with `kid` in the header, so the future AI service can verify offline against
  `/.well-known/jwks.json` without a shared secret.
- Access token 15 minutes. Refresh token 30 days, rotating, stored hashed with a family id.
  Presenting a rotated-out token revokes the whole family and logs a security event.
- Roles `student` / `instructor` / `admin` via `Depends(require_roles(...))`. Never
  `if user.role == "admin"` inline. Ownership checks live in the owning module's service.
- Never log passwords, tokens, keys, full request bodies or PII. Log an identifier, not the object.
- No secret, key or connection string in source, fixtures, tests or commits. Settings only, with
  no default value for any secret.
- Never put real client names, account ids or production data in this repo. Use `[Client A]`,
  `{{api_key}}` and seeded fakes.

---

## 11. Local development

`make dev` runs the API with **auto-reload**:
`uvicorn src.main:app --reload --reload-dir src --host 0.0.0.0 --port 8000`.

In Docker, the dev compose file mounts `./src` into the container and the API service runs that
same command, so editing a file on the host restarts the server inside the container. Install
`watchfiles` — without it uvicorn falls back to slow polling and reload feels broken. Reload is
**dev only**; the production image never sets `--reload`.

```bash
make dev        # api with auto-reload + the infra it needs
make up         # full stack, no reload
make down
make logs s=api
make migrate    # alembic upgrade head
make seed       # idempotent demo data
make test
make check      # ruff + mypy + tests — run before every commit
```

Tooling: `uv` for dependencies, `ruff` for lint and format (replaces black, isort, flake8),
`mypy` on `src/`, `pytest` + `pytest-asyncio` + `testcontainers`.

---

## 12. Coding standards

1. Type hints on every signature.
2. No business logic in `routes.py`. Routes validate, call the service, return.
3. No raw dicts crossing a layer boundary. Pydantic schemas in and out.
4. Async all the way down. One blocking call stalls the event loop — if a library is sync-only,
   run it in a thread pool explicitly and say why.
5. No bare `except:`. No silent `pass` in an exception handler.
6. Functions do one thing. If describing it needs "and", split it.
7. Comment the *why*. A comment restating the code is noise.
8. Conventional commits: `feat:`, `fix:`, `chore:`, `refactor:`, `test:`, `docs:`.

**Testing.** `tests/` mirrors `src/modules/` one-to-one. Unit tests touch no I/O. Integration
tests use testcontainers against real Postgres, Mongo and Redis, never a mocked database. Test
behaviour, not implementation — a test that only asserts a mock was called tests nothing. Every
bug fix starts with a failing test.

---

## 13. What not to do

- Do not create a file, folder or class "for later". Build it when a feature needs it.
- Do not create an abstraction with one implementation.
- Do not create a file whose body is only `pass` or a TODO.
- Do not add a base class, factory or generic wrapper until two concrete cases exist.
- Do not add a dependency without justifying it in the PR description.
- Do not add middleware that nothing exercises.
- Do not split a module into subfolders before a file is genuinely too long.
- Do not maintain a central list of routes, models or modules that must be hand-edited.
- Do not leave commented-out code, dead branches, or TODOs without an owner.
- Do not use `# type: ignore` without a reason on the same line.
- Do not invent business rules. If the spec is ambiguous, ask.
- Do not build AI, embedding or vector-search features here.
- Do not reformat or refactor files unrelated to the change you were asked to make.

---

## 14. Definition of done for any change

- [ ] `make check` passes
- [ ] The module follows the anatomy in §4 and is picked up by auto-discovery
- [ ] No file was created that nothing imports
- [ ] New config has an entry in `.env.example` with no real value
- [ ] New endpoints return the standard envelope and appear correctly in OpenAPI
- [ ] A decision future-you would question has a short ADR in `docs/adr/`