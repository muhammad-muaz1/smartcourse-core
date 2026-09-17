# CLAUDE.md — SmartCourse Core

Standing context for every session in this repository. Read this before writing code.

---

## 1. What this project is

SmartCourse is the backend for a large-scale learning platform (universities, enterprises,
training academies). This repository, `smartcource-core`, is the entire platform backend:
course management, publishing, enrollment, progress, notifications and analytics.

It is a **modular monolith** with multiple process types built from one image:
`api`, `celery-worker`, `celery-beat`, `temporal-worker`, `kafka-consumer`, `outbox-relay`.

The business problems it exists to solve, in the client's words:

- Content publishing is slow and manual.
- Course data, user progress and analytics are scattered and inconsistent.
- High traffic causes delays in enrollments, notifications and background tasks.
- Background workflows are unreliable and failures are hard to diagnose.

Read that list as an engineering brief: **consistency, throughput, recoverability and
observability are the product.** CRUD is the easy part.

An AI layer (contextual Q&A, semantic search, content generation) is planned as a separate
service in a future phase. Do **not** build it here. Do not add LLM SDKs, embedding code or a
vector database to this repository. Where content is chunked for later indexing, store the
chunks and stop there.

---

## 2. Tech stack — do not substitute

| Concern | Choice | Notes |
| --- | --- | --- |
| Language | Python 3.12 | `src/` layout |
| Web | FastAPI + Uvicorn | async throughout |
| Packaging | `uv` | lockfile committed |
| Relational DB | PostgreSQL 16 | system of record |
| ORM | SQLAlchemy 2.0 async (`asyncpg`) | typed `Mapped[]` style |
| Migrations | Alembic (async env) | |
| Document DB | MongoDB 7 | open-shaped + derived data |
| ODM | Beanie + Pydantic v2 | see §4 note on the driver |
| Cache / locks / limits | Redis 7 (`redis.asyncio`) | |
| Task queue | Celery 5 + RabbitMQ broker, Redis results | |
| Event bus | Kafka + Confluent Schema Registry, `confluent-kafka` client, Avro | |
| Workflows | Temporal (`temporalio`) | |
| Tracing | OpenTelemetry → OTLP → Jaeger | |
| Metrics | Prometheus + Grafana | |
| Logging | `structlog`, JSON | |
| Lint + format | `ruff` | replaces black, isort, flake8 |
| Types | `mypy --strict` on `src/` | |
| Tests | `pytest`, `pytest-asyncio`, `testcontainers`, `polyfactory` | |
| Runtime | Docker + Docker Compose | |

Do **not** use SQLModel. One class serving as both table and API schema couples the wire
format to storage, has weaker relationship typing, and you drop to raw SQLAlchemy for anything
non-trivial anyway. SQLAlchemy models and Pydantic schemas are separate objects with explicit
mapping. That separation is the point.

Adding any dependency not listed above requires a one-line justification in the PR description.

---

## 3. Folder structure

```
src/smartcourse/
├── main.py                 # uvicorn entry, thin
├── app_factory.py          # create_app(): middleware, routers, handlers, lifespan
├── core/                   # framework-level concerns, no domain knowledge
│   ├── config/             # nested pydantic-settings, fail-fast validation
│   ├── context.py          # contextvars: request_id, user_id, trace_id, tenant_id
│   ├── logging/            # structlog setup + context injection
│   ├── telemetry/          # OTel tracing, metrics registry, instrumentors
│   ├── errors/             # exceptions.py, codes.py, handlers.py, problem_details.py
│   ├── responses/          # envelope.py, pagination.py (cursor), meta.py
│   ├── middleware/         # one file per middleware
│   ├── security/           # password, jwt (RS256 + JWKS), tokens, rbac, permissions
│   ├── dependencies/       # current_user, require_permissions, sessions, pagination
│   └── utils/              # clock.py, ids.py — not a junk drawer
├── db/
│   ├── postgres/           # engine, session, Base, mixins, unit_of_work, repository
│   ├── mongo/              # client, document base, index registration, repository
│   └── redis/              # client, cache, locks, rate_limiter (Lua)
├── messaging/
│   ├── events/             # envelope schema, registry, catalogue
│   ├── outbox/             # model, writer, relay
│   ├── inbox/              # processed_events model, dedupe decorator
│   ├── kafka/              # producer, consumer base, schema_registry, topics, dlq
│   └── celery/             # app, queues, base_task, beat_schedule
├── workflows/              # Temporal: client, worker, interceptors, definitions, activities
├── modules/                # one folder per bounded context
│   ├── identity/           # reference implementation — copy its shape
│   ├── courses/
│   ├── content/
│   ├── enrollments/
│   ├── progress/
│   ├── notifications/
│   └── analytics/
├── api/                    # router aggregation, health, meta, openapi customisation
└── cli/                    # typer: create-admin, run relay, run consumers, migrate
```

Every module has the same internal shape. No exceptions, no creative variations:

```
modules/<name>/
├── api/          routes.py, schemas.py, dependencies.py
├── domain/       entities.py, errors.py, policies.py
├── models.py     SQLAlchemy models (or documents.py for Mongo-backed modules)
├── repository.py
├── service.py
├── events.py     event definitions this module emits
└── tasks.py      Celery tasks this module owns
```

Top level: `contracts/` (Avro schemas, exported OpenAPI), `deploy/` (docker, compose,
prometheus, grafana, otel), `migrations/` (alembic, mongo), `scripts/`, `docs/`
(adr, prd, architecture, runbooks), `tests/`.

---

## 4. Where data lives

**PostgreSQL is the system of record.** Anything with an invariant that must hold at commit
time lives there: users, roles, refresh tokens, courses, modules, enrollments, progress,
certificates, outbox, inbox, idempotency keys.

**MongoDB holds open-shaped and derived data:** lesson content bodies, extracted assets,
content chunks, publish-run records, notification payloads, analytics projections, event
archive. All of it is rebuildable from Postgres plus the event log.

**Redis holds nothing durable:** cache, rate-limit counters, distributed locks, idempotency
response cache, Celery results.

Rules:

- Invariants are database constraints, not application `if` statements. Duplicate enrollment
  is prevented by a unique constraint on `(student_id, course_id)`, not by a `SELECT` first.
- Seat limits and prerequisites are checked inside a transaction with row locking
  (`SELECT … FOR UPDATE`). A check-then-insert without a lock is a race, and under the load
  this platform expects it will fire.
- No foreign keys across stores. Mongo documents reference Postgres rows by UUID.
- Never write to two stores in one logical operation without going through the outbox.
- `datetime.now(tz=UTC)` only. Never `utcnow()`. Never a naive datetime in a model.

> **Driver check:** Motor is on a deprecation path in favour of PyMongo's native async client,
> and Beanie has historically depended on Motor. Before writing Mongo code, verify Beanie's
> current driver and record the finding in `docs/adr/`. If it still depends on a deprecated
> driver, use a thin typed repository over `pymongo.AsyncMongoClient` instead.

---

## 5. Choosing an async mechanism

Three async systems in one stack. Without a rule, the same work ends up in all three. This is
the rule:

| Use | When | Never for |
| --- | --- | --- |
| **Temporal** | Multi-step process that must complete or compensate, spans minutes to hours, must survive a restart mid-flight | Anything on the request path; single-step work |
| **Kafka** | Broadcasting a fact that already happened to unknown consumers; the analytics log | Commands to a known recipient; work queues |
| **Celery** | One self-contained retryable unit of work, seconds not hours — send an email, recompute a counter | Orchestrating multi-step processes |

**Temporal orchestrates. Kafka informs. Celery executes.**

Course publishing is a Temporal workflow: it returns `202` with a run id, owns the step
sequence and compensation, and writes the terminal status plus an outbox row in one
transaction. Notifications triggered by a publish are Celery tasks fired from a Kafka
consumer — a mail outage must never fail a publish.

### Kafka rules

- **The API never produces to Kafka directly.** It writes outbox rows. The relay is the only
  producer. This is what makes cross-system consistency real rather than aspirational.
- Topics: `smartcourse.<domain>.<event>.v<major>`, e.g. `smartcourse.course.published.v1`.
- Partition key is always the aggregate id, so per-course and per-user ordering holds.
- Schema Registry compatibility is `BACKWARD`; CI fails a PR that breaks it.
- Every consumer: manual commit after success, bounded in-flight work, dedupe by `event_id`
  against the inbox table, DLQ topic (`<topic>.dlq`) carrying payload, error, stack and
  attempt count.

### Celery rules

- Separate queues with separate workers: `default`, `notifications`, `analytics`,
  `maintenance`. One slow queue must never starve the others.
- `acks_late=True`, `task_reject_on_worker_lost=True`, explicit `soft_time_limit` and
  `time_limit` on every task, exponential backoff with jitter, bounded `max_retries`,
  `prefetch_multiplier=1` for long tasks.
- Celery is synchronous; the rest of the app is async. Do not scatter `asyncio.run()` through
  tasks. Inherit the single `BaseTask` in `messaging/celery/base_task.py` that owns one
  persistent loop and the async resource handles.

### Temporal rules

- Workflow code is deterministic: no I/O, no `datetime.now()`, no `random`, no DB access.
  All of that goes in activities.
- `workflow.uuid4()` and `workflow.now()` inside workflows.
- Every activity has `start_to_close_timeout` and a `RetryPolicy` with
  `non_retryable_error_types` for validation failures. Long activities heartbeat.
- Activities are idempotent — they will be retried.
- Version workflow changes with `workflow.patched()` so in-flight runs survive a deploy.

---

## 6. Reliability patterns — already built, always used

**Transactional outbox.** Any state change that emits an event writes the domain row and the
`outbox_messages` row in the *same* transaction. The relay polls with
`FOR UPDATE SKIP LOCKED` and publishes. A partial index on `(published_at) WHERE published_at
IS NULL` is not optional — without it the relay table-scans within a week.

**Three distinct idempotency mechanisms. Do not collapse them.**

1. *Consumer inbox* — `processed_events(event_id, consumer_group)`; insert-or-skip before
   handling. This is what prevents double-processing.
2. *HTTP idempotency* — `Idempotency-Key` header on every unsafe verb. Key plus body hash maps
   to a stored response in Redis, 24h TTL. Same key + same body replays; same key + different
   body returns `409`.
3. *Natural-key constraints* — the storage layer makes the duplicate impossible regardless of
   what the application does.

**Backpressure.** Bounded consumer concurrency, RabbitMQ prefetch limits, queue-per-priority,
DLQ on every consumer. Nothing consumes unboundedly into the database.

**Failures are data, not log noise.** The analytics module reports "Failed Events / Workflow
Issues", so DLQ contents and workflow failures must be queryable, not just greppable.

---

## 7. API conventions

- Versioned prefix `/api/v1`. Routes are plural nouns; verbs live in the HTTP method.
- Success: `{"data": ..., "meta": {...}}`. Never a bare dict, never a bare list.
- Errors: RFC 9457 Problem Details — `type`, `title`, `status`, `detail`, `instance`, plus
  `code` (stable machine-readable string), `errors[]` for field-level validation,
  `request_id`, `trace_id`.
- One handler per exception class, registered once in the app factory. No endpoint builds an
  error body by hand.
- Pagination is cursor-based by default (opaque, base64). Offset only where a UI genuinely
  needs page numbers. Retrofitting cursors later is a breaking change for every client.
- Long operations return `202 Accepted` with a status location, never block the request.
- Every response carries `X-Request-ID`.

Exception hierarchy: `AppError` → `DomainError` (`NotFoundError`, `ConflictError`,
`ValidationError`, `BusinessRuleViolation`) and `InfraError` (`UpstreamError`, `TimeoutError`,
`RateLimitError`). HTTP mapping lives in the handler. **Domain code never imports `fastapi`.**

### Middleware order — order is behaviour, not style

1. `RequestIDMiddleware` — accept or mint `X-Request-ID` into a contextvar
2. OpenTelemetry ASGI instrumentation
3. `StructuredLoggingMiddleware` — one access line with duration and trace id
4. `SecurityHeadersMiddleware`
5. `CORSMiddleware` — explicit origins from settings, never `*` outside local
6. `TrustedHostMiddleware`, `GZipMiddleware`
7. `BodySizeLimitMiddleware`
8. `RateLimitMiddleware` — Redis sliding window via Lua so the check is atomic
9. `IdempotencyMiddleware`

Authentication is **not** middleware. It is a dependency, so OpenAPI documents which routes
need it and unauthenticated routes are explicit rather than accidental.

---

## 8. Security

- Passwords: Argon2id. (`passlib` is effectively unmaintained — use `argon2-cffi` or `pwdlib`.)
- JWT **RS256**, not HS256, with `kid` in the header and rotation supported from day one. The
  future AI service will verify offline against `/.well-known/jwks.json`; a shared secret would
  make that impossible.
- Access token 15 minutes. Refresh token 30 days, rotating, stored hashed with a family id.
  Presenting a rotated-out token revokes the whole family and emits a security event. That rule
  turns token theft from a breach into an alert.
- RBAC: `Role` (student, instructor, admin) mapped to a `Permission` enum. Endpoints declare
  `Depends(require_permissions(Permission.COURSE_PUBLISH))`. Never `if user.role == "admin"`
  inline. Resource ownership ("does this instructor own this course") lives in a policy module.
- Never log passwords, tokens, keys, full request bodies or PII. Log an identifier, not the
  object.
- No secret, key, token or connection string in source, fixtures, tests or commits. Settings
  only, with no default value for any secret.
- Never put real client names, account ids or production data anywhere in this repository.
  Use `[Client A]`, `{{api_key}}` and seeded fakes.

---

## 9. Observability

`structlog` JSON logs only, with `trace_id`, `span_id`, `request_id` and `user_id` injected
automatically — never pass them manually.

Trace context must cross process boundaries or the exercise is theatre: inject `traceparent`
into Kafka message headers and extract in consumers; same for Celery task headers and Temporal
interceptors. A publish that starts in the browser and ends in a worker is **one trace**.

Metrics from day one: request rate, error rate, latency histogram, queue depth, consumer lag,
outbox backlog age, DLQ size, workflow failure count. Grafana dashboards are provisioned as
code in `deploy/grafana/`.

Health endpoints:

- `/health/live` — process only, no dependency checks. A liveness probe that fails on a
  dependency outage restarts healthy containers and makes the outage worse.
- `/health/ready` — Postgres, Mongo, Redis, broker reachability, per-check timeout, ~5s cache.
- `/health/startup` — migrations applied.
- `/version` — git SHA, build time.

---

## 10. Coding standards

1. Type hints on every signature. `mypy --strict` passes.
2. No business logic in routers. Routers parse, delegate to a service, return.
3. No raw dicts crossing a layer boundary. Pydantic schemas in and out.
4. Layering: `api → service → repository → model`, dependencies inward only. `core/` and `db/`
   never import from `modules/`. Modules never import each other's internals — they communicate
   through events or a published service interface.
5. Async all the way down. One blocking call in an async path stalls the event loop; if a
   library is sync-only, run it in a thread pool explicitly and say why.
6. No bare `except:`. No silent `pass` in an exception handler. Ever.
7. Every unsafe endpoint is idempotent, or documents in its docstring why it cannot be.
8. Functions do one thing. If you need "and" to describe it, split it.
9. Comment the *why*. A comment restating the code is noise.
10. Conventional commits (`feat:`, `fix:`, `chore:`, `refactor:`, `test:`, `docs:`).

### Testing

- New code ships with tests.
- Unit tests touch no I/O and test the domain.
- Integration tests use testcontainers against real Postgres, Mongo and Redis — never a mocked
  database.
- Test behaviour, not implementation. A test that only asserts a mock was called tests nothing.
- Every bug fix starts with a failing test.

---

## 11. What not to do

- Do not add a dependency without justifying it in the PR description.
- Do not create an abstraction with one implementation.
- Do not create a file whose body is only `pass` or a TODO.
- Do not leave commented-out code, dead branches, or TODOs without an owner.
- Do not use `# type: ignore` without a reason on the same line.
- Do not invent business rules. If the spec is ambiguous, ask — a guess that reaches production
  becomes a requirement nobody agreed to.
- Do not build AI, embedding or vector-search features here.
- Do not weaken a test to make it pass.
- Do not reformat or refactor files unrelated to the change you were asked to make.

---

## 12. Commands

```bash
make up          # full stack: infra + observability + app processes
make down
make logs s=api
make migrate     # alembic upgrade head + mongo migrations
make seed        # idempotent demo data
make test        # unit + integration
make lint        # ruff check + ruff format --check
make types       # mypy --strict src/
make check       # lint + types + test — run this before every commit
make shell       # python shell with app context
```

---

## 13. Definition of done for any change

- [ ] `make check` passes
- [ ] New endpoints return the standard envelope and are documented in OpenAPI
- [ ] State changes that emit events write the outbox row in the same transaction
- [ ] New consumers dedupe and have a DLQ path
- [ ] New config has an entry in `.env.example` with no real value
- [ ] Anything operationally risky has a runbook in `docs/runbooks/`
- [ ] A decision that future-you would question has an ADR in `docs/adr/`