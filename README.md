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
| `services/course` | 8002 | `course_db` | Course catalogue CRUD: create (instructor), browse/search (public), read, update and archive (owner or admin) |

Shared mechanics (response envelope, error shape, logging, middleware, JWT verification,
DB sessions, health) live in `libs/platform-core`. It holds no business logic.

Content, learning, notification and analytics services come later. None has been built
yet, so none is scaffolded.

## Prerequisites

- Python 3.12 and [`uv`](https://docs.astral.sh/uv/)
- PostgreSQL 16+ and Redis 7+, installed locally, **or** Docker (for `make up`)

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
make dev                 # gateway + every service, auto-reload; Ctrl+C stops all
```

Each service reads only its own `.env` (`services/<name>/.env`, `gateway/.env`). Edit the
`SMARTCOURSE_POSTGRES__DSN` in each to match your local Postgres. The keys you need are
listed in each `env.example`.

### Verify

```bash
curl localhost:8000/health    # gateway: is every service up?
curl localhost:8001/health    # identity: its Postgres + Redis
curl localhost:8002/health    # course: its Postgres
```

`/health` and `/version` sit at each service's root, outside `/api/v1`. They are
infrastructure endpoints and don't change when the API version does. Each service's
interactive docs are at `/docs` on its own port (`localhost:8001/docs`,
`localhost:8002/docs`).

### Everything in Docker instead

```bash
make keys      # once: the containers mount secrets/
make up        # Postgres, Redis, both services, gateway (migrations run on start)
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

## Layout

```
gateway/                  # routing + coarse auth at the edge; no data
services/
├── identity/             # CLAUDE.md, pyproject, Dockerfile, alembic.ini, migrations/, scripts/
│   └── src/  main.py app.py config.py core/ db/ modules/{auth,users}
└── course/
    └── src/  main.py app.py config.py core/ db/ modules/courses
libs/platform-core/       # platform_core: mechanics shared by every service
deploy/                   # docker-compose.yml, Postgres init
scripts/                  # repo-level: create_databases, cutover_from_monolith (one-off)
docs/adr/                 # decisions worth knowing the "why" behind
```

Every service has the same internal shape (root `CLAUDE.md` §5). Adding a module means
creating a folder under `src/modules/`. Its `routes.py` is picked up automatically.

## Commands

`s=<name>` targets one of `identity`, `course`, `gateway`. Without it, a command runs on all of them.

```bash
make dev [s=course]      # run with auto-reload
make migrate [s=course]  # alembic upgrade head
make setup               # env + keys + db + migrate, for a fresh machine
make cutover             # one-off move from the monolith
make env                 # per-service .env from env.example (never overwrites)
make keys                # dev JWT keypair into secrets/ (never overwrites)
make db                  # create each service's database if missing
make create-admin EMAIL=... NAME="..."
make test [s=course]     # unit + integration (integration needs Docker)
make lint                # ruff check + format --check
make types               # mypy --strict, per member
make check               # lint + types + test — before every commit
make up [s=course] / make down / make logs [s=course]   # Docker
```

## Testing

Unit tests touch no I/O. Integration tests use
[testcontainers](https://testcontainers.com/) against real Postgres and Redis, never a
mocked database, so `make test` needs a running Docker daemon for the full suite. Without
Docker, the integration tests error at setup and the unit tests still run.

Tests live beside the code they test: `services/<name>/tests/`, `gateway/tests/`,
`libs/platform-core/tests/`. Each suite runs from its own directory.

## Decisions worth reading before changing infrastructure

- **0010**: the service split. Why Mongo is gone, why course has no foreign key to users,
  why a logout takes up to 15 minutes to reach services other than identity, and why the
  gateway doesn't reject expired tokens.
- **0003**: Starlette middleware is LIFO. `platform_core.middleware.install_middleware`
  encodes the order once.
- **0008**: why `auth` may import `users` directly, and why reuse detection commits
  before raising.
- **0009**: a module folder without `__init__.py` silently never gets its routes mounted.
