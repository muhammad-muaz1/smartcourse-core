# 0010 — Split the monolith into identity, course and a gateway

- Status: Accepted
- Date: 2026-09-28

## Context

The monolith (one FastAPI app, one database, one `.env`) had `auth`, `users` and `courses`
modules. The architecture moved to independently deployable services with a database
each (root `CLAUDE.md` §2–§6). This records what the first cut contains, and the choices a
later reader would otherwise have to reverse-engineer.

## What exists after the split

| Deployable | Port | Database | Contents |
| --- | --- | --- | --- |
| `services/identity` | 8001 | `identity_db` | `auth` + `users` modules, token issuing, JWKS, `create_admin`, key generation |
| `services/course` | 8002 | `course_db` | `courses` module |
| `gateway` | 8000 | none | routing, coarse token check, request ids, CORS at the edge |
| `libs/platform-core` | — | — | envelope, errors + handlers, logging, middleware, routing discovery, JWT verification, DB mechanics, health |

`content`, `learning`, `notification` and `analytics` are **not** scaffolded. None of them
has any code yet, and an empty service is exactly the "for later" folder `CLAUDE.md` §5
rules out. `libs/contracts` waits for the first event for the same reason.

Clients see no change of address: the gateway listens on :8000 and serves `/api/v1/...`
and `/.well-known/jwks.json` exactly as the monolith did.

## Decisions

**MongoDB is gone.** The architecture is PostgreSQL-only (`CLAUDE.md` §6) and nothing had
ever been stored in Mongo — it only appeared in `/health`. Removing it removed `beanie`,
`pymongo` and ADR 0002/0005's Mongo concerns from the running code (the ADRs stay as
history).

**`courses.instructor_id` lost its foreign key.** Users live in `identity_db`; a foreign key
cannot cross databases, and `CLAUDE.md` §6 forbids one across service boundaries anyway. The
column is still indexed. Course tests use a bare UUID for the instructor instead of
creating a `User`.

**Services verify tokens with identity's public key file, not by fetching JWKS.**
`CLAUDE.md` §3 allows exactly one synchronous cross-service call — the gateway fetching
JWKS. So services load the public key from disk (`secrets/jwt_public.pem` locally, a
read-only mount in compose) and never call identity. The course container is given only
the public key.

**Logout is immediate in identity, and bounded everywhere else.** The access-token
denylist lives in identity's Redis DB. Having course read it would be one service reading
another's store. So after logout an access token is rejected by identity at once, and by
course when it expires — at most 15 minutes. If that window becomes a product problem, the
fix is a `session.revoked` event feeding each service's local denylist, not a shared store.

**The gateway's token check is signature-only.** It rejects tokens that are malformed or
not signed by one of identity's keys, and forwards everything else. It deliberately does
**not** reject expired tokens: clients routinely send their expired access token alongside
`POST /auth/refresh`, and rejecting that at the edge would break refresh. Expiry, roles and
revocation are the services' decision. If identity is unreachable and no keys are cached,
the gateway fails open — the services still verify every token.

**The gateway is a small FastAPI app, not nginx/Traefik.** It reuses `platform-core`'s
error shape, request-id middleware and logging, so an edge error looks exactly like a
service error and carries the same `request_id`. It is a few hundred lines with no state. It
forwards `X-Forwarded-For`; uvicorn trusts that from `127.0.0.1` by default and from the
compose network via `FORWARDED_ALLOW_IPS`, so identity's per-IP throttles still see the
real client.

**Every service imports its own code as `src`.** That matches `CLAUDE.md` §5 and kept the
module code unchanged, but it means no service can be installed as a package — two `src`
packages would collide in one venv. So services are uv workspace members with
`package = false`, each runs from its own directory, and mypy/pytest run per directory.
Only `platform-core` is a real installed package.

**Redis DB 0 stays identity's.** The monolith's lockout, throttle, reset and denylist
keys are all identity concerns and were in DB 0, so keeping identity there means none of
them was invalidated by the split. Course is assigned DB 1 but does not connect yet —
nothing in it uses Redis.

**Identity keeps the monolith's migration history** (`856057a1ea54` → `a8ef86b23e0c`);
the courses revision was removed from it. Course starts a fresh chain (`3c9e1f0a7b21`),
the same table minus the foreign key.

**Existing data is copied, not moved.** `make cutover` (one-off,
`scripts/cutover_from_monolith.py`) splits the repo-root `.env` into per-service `.env`
files, creates the new databases, migrates them, then copies users, refresh tokens and
courses with `ON CONFLICT DO NOTHING`. The legacy database is only read, so the move can
be repeated or abandoned safely. The same keypair is reused, so tokens issued before the
split stay valid.

## Not done in this change

These are real requirements, not dropped ones — each lands with the first feature that
needs it:

- **Transactional outbox and events.** No state change emits an event yet. Register,
  course update and archive are "Simple + Event" in the API design; the outbox table and
  `libs/contracts` arrive with the first consumer (`learning`'s `course_cache`).
- **OpenTelemetry tracing and `/metrics`.** Logs carry `request_id` end to end (the
  gateway forwards it) and `service.name` on every line, but there are no spans yet.
- **Gateway rate limiting.** identity's login/register throttles still apply.
- **`/docs` through the gateway.** Each service serves its own OpenAPI at `/docs` on its
  own port (8001, 8002).

## Consequences

- `GET /health` and `GET /version` moved from `/api/v1/health` to the host root, per API
  design §11. The gateway's `/health` reports each service; each service's reports its
  own dependencies.
- The `mongo` entry is gone from every health response.
- Configuration is per service: `services/<name>/.env` and `gateway/.env`. The repo-root
  `.env` and `env.example` are no longer read by anything except the cutover script.
