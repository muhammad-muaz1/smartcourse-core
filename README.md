# SmartCourse Core

Backend for **SmartCourse**, EduCorp's large-scale learning platform: course
management, publishing, enrollments, progress and analytics for universities,
enterprises and training academies.

It is a monorepo of independently deployable services, each with its own PostgreSQL
database, behind a gateway. Clients see a single API at `http://localhost:8000/api/v1`.

- Business context: [`.claude/Requirement.md`](.claude/Requirement.md)
- Architecture rules every change follows: [`.claude/Claude.md`](.claude/Claude.md). Read it before touching code.
- API surface: [`.claude/Api_design.md`](.claude/Api_design.md)
- Why the code is split the way it is: [`docs/adr/0010`](docs/adr/0010-split-into-services.md)

## What's running

| Deployable | Port | Database | What it does |
| --- | --- | --- | --- |
| `gateway` | **8000** | — | The only address clients use. Routes by path, rejects forged tokens, carries `X-Request-ID` through to the services |
| `services/identity` | 8001 | `identity_db` | Register, login, refresh (rotating, with reuse detection), logout, `/auth/me`, password reset, JWKS |
| `services/course` | 8002 | `course_db` | Catalogue, modules and lessons, course lifecycle (draft → publishing → published/failed, unpublish, archive), publish runs |
| `services/identity` worker | :9101 metrics | `identity_db` | Outbox relay: `user.registered.v1` → Kafka |
| `services/course` worker | :9102 metrics | `course_db` | Temporal worker on `course-tq` (publishing saga) + outbox relay (`course.*`, `workflow.failed`) |
| `services/learning` | 8003 | `learning_db` | Enrol (duplicates, seat limits, published-only), withdraw, progress + lesson completion, roster, history |
| `services/learning` worker | :9103 metrics | `learning_db` | Kafka consumer (`course.*` → `course_cache`, `user.registered` → roster names) + outbox relay (`enrollment.*`) |
| `services/notification` | 8004 | `notification_db` | Notification inbox (`/notifications/me`, read, read-all) |
| `services/notification` worker | :9104 metrics | `notification_db` | Kafka consumer: welcome, course-welcome and completion notifications → Celery |
| `services/notification` Celery | :9114 metrics | `notification_db` | Delivery attempts (late-acked, time-limited, jittered retries) + beat sweep |
| `services/analytics` | 8005 | `analytics_db` | The brief's nine metrics at `/analytics/*`, each with `meta.as_of` |
| `services/analytics` worker | :9105 metrics | `analytics_db` | Kafka consumer over every topic → fact tables |
| Kafka | 9092 | — | The event log. Topics retained forever, so projections can be replayed |
| Temporal dev server | 7233 / UI 8233 | — | Durable workflows: the publishing saga |
| Jaeger | UI 16686, OTLP 4318 | — | Traces: one trace from the gateway to the last consumer and Celery task |
| Prometheus | 9090 | — | Scrapes every API's `/metrics` and every worker's metrics port |

Shared mechanics (response envelope, error shape, logging, tracing, metrics, middleware,
idempotency keys, JWT verification, DB sessions, health, outbox, inbox, the Kafka
consumer runner) live in `libs/platform-core`. It holds no business logic.
`libs/contracts` holds the versioned events services exchange — nothing else — with their
committed schemas and a BACKWARD-compatibility test.

How events flow, and why: [ADR 0012](docs/adr/0012-week3-event-spine-celery-analytics-observability.md).
Content service (chunking, search) comes later.

## Prerequisites

- Python 3.12 and [`uv`](https://docs.astral.sh/uv/)
- PostgreSQL 16+ and Redis 7+, installed locally, **or** Docker (for `make up`)
- The [Temporal CLI](https://docs.temporal.io/cli#install) for `make temporal` (local dev
  server), unless you use `make up`
- Java 17+ for `make kafka` (Kafka runs from the official release, no Docker needed),
  unless you use `make up`

## Getting started

### A machine that ran the old monolith

This is a one-off step. It turns your repo-root `.env` into one `.env` per service,
creates `identity_db` and `course_db`, migrates them, and copies your users, refresh
tokens and courses across. The old database is only read, never changed, and you can
re-run the step safely.

```bash
uv sync --all-packages
make cutover
make dev
```

Your existing JWT keys in `secrets/` are reused, so tokens issued before the split keep
working.

### A fresh machine

```bash
uv sync --all-packages   # one venv for the whole workspace
make setup               # per-service .env files, dev JWT keys, databases, migrations
make temporal            # in its own terminal: Temporal dev server, UI on :8233
make kafka               # in its own terminal: Kafka on :9092 (downloaded on first run)
make jaeger              # in its own terminal: traces, UI on :16686 (optional)
make prometheus          # in its own terminal: metrics on :9090 (optional)
make dev                 # gateway + 5 services + 5 workers + Celery; Ctrl+C stops all
make smoke               # in another terminal: end-to-end check of every event flow
```

Without Kafka the APIs still run and every state change is kept in its service's outbox;
nothing reaches the other services until Kafka is back. Without Temporal, publish runs
stay `pending`. Kafka, Jaeger and Prometheus run from official release binaries kept in
`.tools/` (checksum-verified, gitignored).

### Upgrading a machine from week 2

```bash
uv sync --all-packages
make env       # appends the new settings to your existing .env files; changes nothing else
make db        # creates notification_db and analytics_db
make migrate
```

`make env` creates `services/notification/.env` and `services/analytics/.env` from their
`env.example` — set their `SMARTCOURSE_POSTGRES__DSN` to your local credentials. Then start
Kafka and `make dev`, and run `make backfill` once: users, courses and enrollments created
before week 3 never produced events, and this announces them so analytics and
notification know about them (old facts are recorded, never re-sent as welcomes).

Each service reads only its own `.env` (`services/<name>/.env`, `gateway/.env`). Edit the
`SMARTCOURSE_POSTGRES__DSN` in each to match your local Postgres. The keys you need are
listed in each `env.example`.

### Verify

```bash
curl localhost:8000/health    # gateway: is every service up?
curl localhost:8001/health    # identity: its Postgres + Redis
curl localhost:8002/health    # course: its Postgres + Redis
curl localhost:8003/health    # learning: its Postgres + Redis
curl localhost:8004/health    # notification
curl localhost:8005/health    # analytics
make smoke                    # every event-driven flow, end to end
```

`/health` and `/version` sit at each service's root, outside `/api/v1`. They are
infrastructure endpoints and don't change when the API version does. Each service's
interactive docs are at `/docs` on its own port (`localhost:8001/docs`,
`localhost:8002/docs`).

### Everything in Docker instead

```bash
make keys      # once: the containers mount secrets/
make up        # Postgres, Redis, Temporal, every service + workers, gateway (migrations run on start)
make logs s=identity
make down
```

## Try it

```bash
# Register (student or instructor — admin is never self-assignable)
curl -X POST localhost:8000/api/v1/auth/register -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"correcthorse123","full_name":"You","role":"instructor"}'

# Log in -> {"data": {"access_token": "...", "refresh_token": "...", ...}}
curl -X POST localhost:8000/api/v1/auth/login -H "Content-Type: application/json" \
  -d '{"email":"you@example.com","password":"correcthorse123"}'

curl localhost:8000/api/v1/auth/me -H "Authorization: Bearer <access_token>"

# Courses: create (instructor), browse (public), update / archive (owner or admin)
curl -X POST localhost:8000/api/v1/courses -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{"title":"Intro to Python","description":"Learn the basics","level":"beginner"}'
curl "localhost:8000/api/v1/courses?q=Python&level=beginner&limit=20"
curl -X PATCH localhost:8000/api/v1/courses/<course_id> -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" -d '{"title":"Intro to Python (Updated)"}'
curl -X DELETE localhost:8000/api/v1/courses/<course_id> -H "Authorization: Bearer <access_token>"
```

### Creating an admin account

Self-registration only ever grants `student` or `instructor`. The only way to get an admin:

```bash
make create-admin EMAIL=you@example.com NAME="Your Name"   # prompts for a password
```

## Observability: diagnosing a failure

| Question | Where |
| --- | --- |
| What happened to this request, across every service? | Jaeger (`:16686`). Every log line carries `trace_id`; search it. A request that emits events is **one trace** through the outbox relay, Kafka consumers and Celery task |
| Is an event stuck before Kafka? | `outbox_backlog` / `outbox_oldest_unpublished_age_seconds` on the worker's metrics port; the row's `attempts` and `last_error` in that service's `outbox_messages` |
| Did a consumer give up on an event? | `events_consumed_total{outcome="dead_lettered"}`; `GET /api/v1/analytics/failures?type=dead_letter` (admin) shows the topic, offset and error |
| Did a publish or a delivery fail? | `GET /api/v1/analytics/failures?type=workflow`; the publish run's `failed_step`/`error`; a notification's `status`/`last_error` |
| Is a service slow or erroring? | Prometheus (`:9090`) / Grafana (`:3001` with `make up`): request rate, p95, 5xx by service, event and task outcomes |

Analytics (`/api/v1/analytics/...`, all with `meta.as_of`): `overview` (students,
instructors, published courses), `enrollments?from=&to=&granularity=day|week|month`,
`completion?course_id=`, `time-to-complete?course_id=`, `courses/popular?period=7d|30d|90d|all`,
`students/course-load`, `courses/{id}`, `failures`.

## Layout

```
gateway/                  # routing + coarse auth at the edge; no data
services/
├── identity/             # CLAUDE.md, pyproject, Dockerfile, alembic.ini, migrations/, scripts/
│   └── src/  main.py app.py config.py core/ db/ modules/{auth,users}
├── course/
│   └── src/  main.py worker.py app.py config.py core/ db/ modules/courses
├── learning/
│   └── src/  main.py worker.py app.py config.py core/ db/ modules/enrollments
├── notification/
│   └── src/  main.py worker.py celery_app.py app.py config.py core/ db/ modules/notifications
└── analytics/
    └── src/  main.py worker.py app.py config.py core/ db/ modules/metrics
libs/platform-core/       # platform_core: mechanics shared by every service
libs/contracts/           # smartcourse_contracts: versioned events + committed schemas/
deploy/                   # docker-compose.yml, Postgres init, observability/ (Prometheus, Grafana)
scripts/                  # repo-level: databases, env sync, local infra, e2e smoke, cutover
docs/adr/                 # decisions worth knowing the "why" behind
```

Every service has the same internal shape (root `CLAUDE.md` §5). Adding a module means
creating a folder under `src/modules/`. Its `routes.py`, `consumers.py` (Kafka handlers),
`tasks.py` (Celery), `workflows.py`/`activities.py` (Temporal) and `dispatchers.py` are
picked up automatically.

## Commands

`s=<name>` targets one of `identity`, `course`, `learning`, `notification`, `analytics`, `gateway`. Without it, a command runs on all of them.

```bash
make dev [s=course]      # run with auto-reload
make worker s=course     # one worker process (any service); no reload
make celery              # notification's Celery worker + beat
make temporal            # Temporal dev server (state in .temporal/)
make kafka               # Kafka, no Docker (state in .tools/)
make jaeger              # Jaeger, no Docker — UI :16686
make prometheus          # Prometheus, no Docker — :9090
make backfill            # one-off: announce pre-week-3 users/courses/enrollments
make smoke               # end-to-end check of every event flow, against `make dev`
make migrate [s=course]  # alembic upgrade head
make setup               # env + keys + db + migrate, for a fresh machine
make cutover             # one-off move from the monolith
make env                 # per-service .env from env.example (appends new keys only)
make keys                # dev JWT keypair into secrets/ (never overwrites)
make db                  # create each service's database if missing
make create-admin EMAIL=... NAME="..."
make test [s=course]     # unit + integration (integration needs Docker)
make test-local [s=...]  # the same, without Docker: throwaway Postgres + Redis in .tools/
make lint                # ruff check + format --check
make types               # mypy --strict, per member
make check               # lint + types + test — before every commit
make up [s=course] / make down / make logs [s=course]   # Docker
```

## Testing

Unit tests touch no I/O. Integration tests use
[testcontainers](https://testcontainers.com/) against real Postgres and Redis, never a
mocked database, so `make test` needs a running Docker daemon for the full suite. Without
Docker, `make test-local` runs the same suites against a throwaway Postgres cluster and
Redis it starts under `.tools/` (on ports 55432/56379) and stops afterwards. Never point
the suites at a development database: they drop every table and FLUSHDB between tests.

`libs/contracts` tests are the schema check: a change to an event that isn't BACKWARD
compatible fails them. `make smoke` is the end-to-end check, against the running stack.

Tests live beside the code they test: `services/<name>/tests/`, `gateway/tests/`,
`libs/platform-core/tests/`. Each suite runs from its own directory.

## Decisions worth reading before changing infrastructure

- **0012**: the event spine. Why aiokafka, why no Schema Registry server yet, retries and
  dead letters, why analytics stores facts rather than counters, Celery's per-process
  event loop, and how one trace runs from the gateway to the Celery task.

- **0010**: the service split. Why Mongo is gone, why course has no foreign key to users,
  why a logout takes up to 15 minutes to reach services other than identity, and why the
  gateway doesn't reject expired tokens.
- **0003**: Starlette middleware is LIFO. `platform_core.middleware.install_middleware`
  encodes the order once.
- **0008**: why `auth` may import `users` directly, and why reuse detection commits
  before raising.
- **0009**: a module folder without `__init__.py` silently never gets its routes mounted.
