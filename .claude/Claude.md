# CLAUDE.md — SmartCourse

Root standing context for every session in this repository. Read this before writing code.
Each service also has its own `CLAUDE.md`; this file governs everything above the service line.

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

An AI layer (Q&A, semantic search, content generation) is a separate service, built later.
Never add LLM SDKs, embeddings or a vector database to any service here.

---

## 2. Architecture

**Six services, one monorepo, one database engine.** Each service owns its data, deploys
independently, and communicates through events. Internally every service has the same structure,
so moving between them costs nothing.

| Service | Owns | Scales on |
| --- | --- | --- |
| **identity** | users, roles, refresh tokens, JWKS | login bursts |
| **course** | courses, modules, lessons (metadata **and** body), prerequisites, catalogue, publish orchestration | catalogue reads |
| **content** | derived artifacts: chunks, search index, uploaded assets, extraction | processing spikes |
| **learning** | enrollments, progress, completions, certificates | enrollment spikes — the hot path |
| **notification** | notification records, preferences, delivery | fan-out bursts |
| **analytics** | projections, all reported metrics | dashboard reads |

Plus `gateway` — routing, TLS, coarse auth, rate limiting. Infrastructure, not a business service.
It holds no data and no business rules, ever.

### Why these six, and not more

**`users` is inside `identity`.** A profile and a credential share a lifecycle and a primary key.

**`progress` is inside `learning`.** They share the enrollment aggregate: enrolling initialises
progress, the last completion completes the enrollment, a certificate needs both. Splitting them
turns one transaction into a saga with compensation, for two tables that are always written
together.

**Lesson bodies are inside `course`, not `content`.** A lesson's metadata and its body are created
in one request. Splitting them across services means a dual write with no transaction across it —
a crash between them leaves a lesson pointing at a body that does not exist. For a platform whose
headline complaint is inconsistent data, that is indefensible. `content` therefore owns only
**derived** artifacts (chunks, index, processed assets), all of which are rebuildable from
`course`.

**`publishing` is inside `course`.** The orchestrator and the aggregate owner should be the same
service.

---

## 3. Non-negotiable boundaries

These four rules are what make this architecture worth its cost. Breaking any one of them
produces a distributed monolith: all the coupling, none of the transactions.

1. **No service reads another service's database.** Not for a quick read, not for reporting, not
   for a migration script. A shared database is a shared schema.
2. **No synchronous service-to-service call on a request path.** Exactly one exception exists:
   the gateway fetching JWKS from `identity`, cached. Everything else is events or Temporal
   activities. If you need data from another service on a hot path, you need a local read model
   fed by events.
3. **No shared business logic.** `libs/platform-core` holds mechanics only. A domain model or
   business rule in a shared library means every service redeploys when it changes.
4. **Every cross-service fact is an event with a versioned contract.** Never an ad-hoc JSON blob.

---

## 4. Repository layout

```
smartcourse/
├── CLAUDE.md                  # this file
├── services/
│   ├── identity/              # each has its own CLAUDE.md, pyproject, Dockerfile, migrations
│   ├── course/
│   ├── content/
│   ├── learning/
│   ├── notification/
│   └── analytics/
├── libs/
│   ├── platform-core/         # envelope, errors, logging, telemetry, JWT verify, outbox, inbox, health
│   └── contracts/             # Avro schemas + generated models — the only cross-service coupling
├── gateway/
├── deploy/                    # compose profiles, observability configs
└── docs/                      # adr/, prd/, runbooks/
```

Monorepo, six deployables. Independent means its own image and its own deploy pipeline — not its
own git remote. At six services and one team, atomic contract changes beat repo-level enforcement.

**`platform-core` may contain:** response envelope, exception classes and handlers, structlog
setup, OTel setup, JWT verification, outbox writer, inbox dedupe, health-check framework, session
management. **It may never contain:** an entity, a domain rule, a shared enum of business states.

---

## 5. Service internals — identical everywhere

```
services/<name>/src/
├── main.py                 # entrypoint: uvicorn runner only
├── app.py                  # create_app() — assembles the application
├── config.py               # all settings, one file, pydantic-settings
├── core/                   # thin — most of it comes from platform-core
│   ├── router.py           # auto-discovers every module's routes.py
│   └── dependencies.py     # this service's own dependencies
├── db/
│   ├── base.py             # Base + UUIDMixin + TimestampMixin
│   ├── postgres.py         # engine, session factory, get_session, check()
│   └── redis.py            # client, get_redis, check()
└── modules/                # bounded contexts inside this service
```

Modelled on NestJS: one folder per feature, a small predictable set of flat files inside it.

| NestJS | Here |
| --- | --- |
| `AppModule` | `src/app.py` |
| Module folder | `src/modules/<name>/` |
| Controller | `routes.py` |
| Service / provider | `service.py` |
| Entity | `models.py` |
| DTO | `schemas.py` |

### Module anatomy

```
modules/courses/
├── routes.py        # HTTP: validate, call service, return. Always exists.
├── service.py       # business logic. Exists as soon as there is logic.
├── models.py        # SQLAlchemy models
├── schemas.py       # Pydantic request/response models
├── events.py        # events this module emits
└── consumers.py     # events this module handles — only once it consumes any
```

Add `repository.py` **only** when queries get complex enough that `service.py` is hard to read.
One data-access class per table from day one is ceremony that makes a codebase feel heavy without
making it safer.

**Routing contract.** Every `routes.py` exposes exactly one module-level `router`:

```python
router = APIRouter(prefix="/courses", tags=["courses"])
```

`core/router.py` walks `src/modules/*/routes.py` and includes each `router` under `/api/v1`.
Adding a module means creating a folder — never editing a central registration list. The same
auto-discovery applies to `consumers.py`, `tasks.py` and `workflows.py` as those arrive.

**Layering.** `routes.py → service.py → models.py`, dependencies pointing one way. `core/` and
`db/` never import from `modules/`. Modules never import another module's `service.py` or
`models.py`.

### Three rules that keep this from scattering

1. **A file exists only when something imports it.** No placeholders, no "we'll need this later".
2. **Grow inside a module before growing the tree.** Flat files until one file passes ~300 lines;
   then that *one* file becomes a folder. Never pre-split.
3. **Shared code moves up on the second use, not the first.**

---

## 6. Data — PostgreSQL only

One engine, one set of operational skills, one backup procedure, one migration tool. Each service
has its own database: `identity_db`, `course_db`, `content_db`, `learning_db`,
`notification_db`, `analytics_db`. Separate databases on one instance in dev; separate instances
in production if load demands. **Isolation comes from the boundary, not the vendor.**

**Why not a document store alongside it.** Every candidate use case has a good Postgres answer:
lesson bodies are `jsonb` with a type discriminator and a `CHECK` per type; chunks are a table
with a foreign key and a GIN index; projections are rollup tables written transactionally.
What a second engine actually costs is a dual write with no transaction across it on the most
basic path in the system, plus two backup procedures, two migration mechanisms and two idioms in
one codebase. Document stores win on horizontal write sharding and genuinely unpredictable
document shapes; neither applies here.

**Postgres features this design leans on:** `jsonb` + GIN for open-shaped content, `tsvector` for
catalogue and content search, `FOR UPDATE SKIP LOCKED` for the outbox relay, partial indexes for
unpublished rows, native partitioning for time-series, and `pgvector` in the future AI service.

**Redis holds nothing durable:** cache, rate limits, locks, idempotency responses, counters.
Flush it and the platform is slower, never wrong.

### Rules

- **Invariants are database constraints, not application `if` statements.** Duplicate enrollment
  is prevented by a unique constraint on `(student_id, course_id)`, not by a `SELECT` first.
- **Seat limits use `SELECT … FOR UPDATE`** in the same transaction as the insert. Check-then-
  insert without a lock is a race that fires under exactly the launch traffic we expect.
- **`jsonb` needs constraints.** A `jsonb` column without a type discriminator and a `CHECK` is a
  schema-free dumping ground. Constrain the shape you actually support.
- **Services take a session, they never fetch one.** Every service function's first parameter is
  the session. A consumer or an activity has no HTTP request; a service that acquires its own
  session can only be called from a route.
- One transaction per request, owned by the route's session dependency. Services do not commit;
  they raise or return.
- Timezone-aware UTC only: `datetime.now(tz=UTC)`, never `utcnow()`, never a naive datetime.
- No foreign keys across service boundaries. Reference other services' entities by UUID.

Stack: SQLAlchemy 2.0 async (`asyncpg`) with typed `Mapped[]` models, Alembic per service,
`redis.asyncio`. **Do not use SQLModel** — one class serving as both table and API schema couples
the wire format to storage.

---

## 7. Asynchronous work — three tools, three different problems

The brief contains four distinct needs. Three of them require genuinely different machinery, and
using one tool for another's job is how teams accidentally rebuild a workflow engine.

| Need in the brief | Tool | Why the others can't |
| --- | --- | --- |
| "Partial failures must not corrupt the publishing workflow" — multi-step, must compensate, must survive a restart mid-flight | **Temporal** | A queue retries a task but cannot undo steps 1–3 when step 4 fails, or resume a half-finished sequence after a worker dies. A log has no notion of a process at all. |
| One enrollment fact, three independent reactions, none blocking the others | **Kafka** | A queue deletes the message once consumed — one message, one consumer. Fan-out with independent offsets is a log property. |
| "Dashboards disagree with the actual state" — fixing a projection bug means recomputing history | **Kafka** | Only a retained log lets you reset an offset and replay. With a queue, the messages are gone and the bug is uncorrectable. |
| "Sending notifications" — one retryable unit of work | **Celery** | Temporal for a single email is a workflow history for a one-step job. |

**Temporal orchestrates. Kafka informs. Celery executes.**

Honest ranking if scope is ever cut: Temporal is the one to keep — compensation is the hardest
thing here to hand-roll and the brief asks for it explicitly. Kafka earns its place on replay.
Celery is the weakest of the three and could be a `jobs` table with `SKIP LOCKED`.

### Deployment

**One Kafka cluster. One Temporal cluster. One Redis cluster.** A broker per service is not
isolation — it is six operational burdens with no cross-service event flow. Isolation comes from
separate topics, consumer groups, task queues and databases.

- **Kafka:** each service owns its topics (`smartcourse.<domain>.<event>.v<major>`) and its
  consumer groups. Only the owning service produces to its own topics. Partition key is always
  the aggregate id. Schema Registry compatibility `BACKWARD`, enforced in CI.
- **Temporal:** one namespace per environment. Each service runs its **own worker** on its **own
  task queue** (`course-tq`, `content-tq`). A workflow lives in the service that owns the process,
  and schedules activities onto other services' queues. Neither side holds an HTTP client for the
  other.
- **Celery:** separate queues per concern (`notifications`, `maintenance`), `acks_late=True`,
  explicit `soft_time_limit` and `time_limit` on every task, backoff with jitter, bounded retries.

### Two rules that must hold from the first event

**Transactional outbox.** A state change that emits an event writes the outbox row in the **same
transaction** as the change. The relay is the only producer. This is what makes cross-service
consistency real rather than aspirational, and it is the one thing that cannot be retrofitted
cheaply.

**Consumer inbox dedupe.** Every broker is at-least-once. Every consumer inserts into
`processed_events(event_id, consumer_group)` before handling, and skips on conflict.

---

## 8. Cross-service patterns

**Local read models.** `learning` keeps a `course_cache` table — course id, status, capacity,
prerequisites — fed by `course.*` events. It feels wrong to duplicate data and it is the correct
answer: enrollment needs that data on its hottest path, and a synchronous call to `course` would
couple their uptime and add latency where it hurts most.

The consequence, to be decided with the client rather than discovered: capacity can be seconds
stale, so one enrollment may exceed the limit. Either accept it (a waitlist is usually a product
feature) or have `course` reconcile and emit a correction.

**Sagas.** Publishing is a Temporal workflow owned by `course`, with `content` activities on
`content-tq`. Any failure compensates: remove partial chunks, roll status back to `failed`, record
the failing step.

**Denormalised snapshots.** A roster needs student names. `learning` stores a name snapshot at
enrollment time rather than calling `identity`. Stale names are acceptable; a hard dependency on
another service for a list view is not.

---

## 9. Authentication across services

- `identity` is the only issuer. Passwords: Argon2id (`argon2-cffi` or `pwdlib`; `passlib` is
  unmaintained).
- JWT **RS256** with `kid` in the header. Every service verifies **offline** against the cached
  JWKS. No introspection call, no shared secret. This is what lets existing users keep enrolling
  when `identity` is down.
- Access token 15 minutes. Refresh token 30 days, rotating, stored hashed with a family id.
  Presenting a rotated-out token revokes the whole family and logs a security event.
- The gateway verifies and rejects garbage early. **Services verify again independently** — never
  trust the gateway alone, or anything reaching a pod directly bypasses auth entirely.
- Service-to-service tokens carry `aud: <target-service>` and a service subject type.
- Roles `student` / `instructor` / `admin` via `Depends(require_roles(...))`. Never
  `if user.role == "admin"` inline. Ownership checks live in the owning service.

---

## 10. Requests and responses

- Versioned prefix `/api/v1`. Plural nouns; verbs live in the HTTP method.
- Success: `{"data": ..., "meta": {...}}` from `platform-core`. Never a bare dict or list.
- Errors: one shape, built only by the shared handler —
  `{"code": "COURSE_NOT_FOUND", "message": "...", "details": [...], "request_id": "..."}`.
  No route ever constructs an error body.
- Pagination is cursor-based (`?limit=&cursor=`), `meta.next_cursor` in the response.
- Long operations return `202` with a status URL.
- Reads served from a projection carry `meta.as_of`, so clients handle staleness by default.

Exceptions: `AppError` → `NotFoundError`, `ConflictError`, `ValidationError`, `UnauthorizedError`,
`ForbiddenError`, `BusinessRuleError`, each with a stable `code`. HTTP mapping lives in the
handler — **service code never imports `fastapi`**.

---

## 11. Health and observability

**One route per service: `GET /health`.** Overall status plus a per-dependency breakdown, `200`
healthy, `503` not. Each dependency exposes a `check()`; health calls them concurrently with a
per-check timeout, cached ~5s. Adding Kafka or Temporal later is one entry in that list.

`GET /version` (git SHA, build time) stays separate. `GET /metrics` for Prometheus. No
`/health/live`, `/health/startup` or `/health/db`.

> At deployment: point the Kubernetes liveness probe at `/version`, not `/health` — otherwise a
> Redis blip restarts healthy pods and turns a degradation into an outage.

**Tracing is load-bearing here, not decorative.** With six services, a failure with no trace id is
unfixable. Trace context must be injected into Kafka message headers, Celery task headers and
Temporal interceptors. One publish must be one span tree in Jaeger, browser to worker. There is a
CI test asserting this, because it breaks silently.

Every log line carries `request_id`, `trace_id` and `service.name`, injected automatically — never
passed manually. Never log passwords, tokens, keys, full request bodies or PII. Log an identifier,
not the object.

---

## 12. Middleware

Registered in `app.py` in this order, from `platform-core`:

1. **`RequestContextMiddleware`** — accepts or mints `X-Request-ID` into a contextvar, logs one
   line per request with method, path, status, duration.
2. **`CORSMiddleware`** — explicit origins from settings, never `*` outside local.

That is the whole stack today. Rate limiting, idempotency keys, body-size limits and security
headers each get added **when the endpoint that needs them is built**, with a test that proves it
works. Middleware that runs on every request and is exercised by nothing costs latency and hides
bugs.

Rate limiting and idempotency are on the milestone plan with dates, not left to judgement — both
are stated requirements and both tend to get noticed only after an incident.

Authentication is **not** middleware. It is a dependency, so OpenAPI documents which routes need
it and unauthenticated routes are explicit rather than accidental.

---

## 13. Local development

```bash
make up                 # everything: all services + infra
make up s=learning      # one service + its database + shared infra
make dev s=learning     # that service with auto-reload
make down
make logs s=learning
make migrate s=learning # alembic upgrade head for one service
make seed
make test               # affected services only
make check              # ruff + mypy + tests — before every commit
```

`make dev` runs `uvicorn src.main:app --reload --reload-dir src`. In Docker the dev compose file
mounts `./services/<name>/src` into the container and runs the same command. Install `watchfiles`
or uvicorn falls back to slow polling. Reload is **dev only**; the production image never sets
`--reload`.

Six services on a laptop is heavy — compose profiles are not optional.

Tooling: `uv` workspaces, `ruff` for lint and format, `mypy`, `pytest` + `pytest-asyncio` +
`testcontainers`.

---

## 14. Testing and CI

`tests/` mirrors `src/modules/` one-to-one inside each service. Unit tests touch no I/O.
Integration tests use testcontainers against a real Postgres, never a mocked database. Test
behaviour, not implementation — a test that only asserts a mock was called tests nothing. Every
bug fix starts with a failing test.

CI uses change detection so only affected services build. Three checks are specific to this
architecture and are not optional:

- **Contract compatibility** — a producer change that breaks `BACKWARD` fails the PR.
- **Trace propagation** — assert `traceparent` survives Kafka headers and Temporal interceptors.
- **Boundary lint** — no service imports another service's code; no HTTP client to an internal
  host outside the allowed list. This is the rule that erodes silently and invisibly.

---

## 15. Coding standards

1. Type hints on every signature.
2. No business logic in `routes.py`.
3. No raw dicts crossing a layer boundary. Pydantic schemas in and out.
4. Async all the way down. One blocking call stalls the event loop — if a library is sync-only,
   run it in a thread pool explicitly and say why.
5. No bare `except:`. No silent `pass` in an exception handler.
6. Functions do one thing. If describing it needs "and", split it.
7. Comment the *why*. A comment restating the code is noise.
8. Conventional commits, scoped by service: `feat(learning):`, `fix(course):`.

---

## 16. What not to do

- Do not read another service's database. Ever.
- Do not add a synchronous call between services on a request path.
- Do not put a domain model or business rule in `libs/platform-core`.
- Do not create a file, folder or class "for later".
- Do not create an abstraction with one implementation.
- Do not create a file whose body is only `pass` or a TODO.
- Do not add a base class, factory or generic wrapper until two concrete cases exist.
- Do not add a dependency without justifying it in the PR description.
- Do not add middleware that nothing exercises.
- Do not maintain a central list of routes, models or modules that must be hand-edited.
- Do not emit an event without a registered schema.
- Do not use `# type: ignore` without a reason on the same line.
- Do not invent business rules. If the spec is ambiguous, ask.
- Do not build AI, embedding or vector-search features in these services.
- Do not put real client names, account ids or production data in this repo. Use `[Client A]`,
  `{{api_key}}` and seeded fakes.
- Do not reformat or refactor files unrelated to the change you were asked to make.

---

## 17. Definition of done for any change

- [ ] `make check` passes for the affected service
- [ ] The module follows §5 and is picked up by auto-discovery
- [ ] No file was created that nothing imports
- [ ] No new cross-service dependency, or if there is one, it is an event with a schema
- [ ] State changes that emit events write the outbox row in the same transaction
- [ ] New consumers dedupe via the inbox and have a DLQ path
- [ ] New config has an entry in `.env.example` with no real value
- [ ] New endpoints return the standard envelope and appear correctly in OpenAPI
- [ ] A decision future-you would question has a short ADR in `docs/adr/`