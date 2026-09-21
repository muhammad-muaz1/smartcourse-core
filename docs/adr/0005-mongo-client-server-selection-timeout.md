# 0005 — Bound PyMongo's server selection timeout at the client level

- Status: Accepted
- Date: 2026-09-17

## Context

While building Phase 2's lifespan wiring, running the app with no reachable MongoDB
caused startup to hang for ~30 seconds and then **fail entirely** — the whole process
exited instead of starting up with Mongo simply marked unhealthy. This directly
contradicted the design goal (carried over from Phase 1 / ADR 0004) that a dependency
outage must never prevent the app from starting, only flip `/health/ready`.

## Root cause

`db.mongo.index_registration.init_mongo` calls `beanie.init_beanie(...)`, which — even
with an empty `document_models` list — unconditionally runs a `{"buildInfo": 1}` command
against the server to cache its version info. That call is not wrapped in any timeout of
ours, and PyMongo's own default `serverSelectionTimeoutMS` is 30000 (30s). The
`asyncio.timeout()` wrapper in `core/health.run_check` only bounds calls made *through*
`CachedHealthCheck` (the `/health/ready` pings); `init_mongo` is called directly in the
lifespan and isn't one of those.

## Decision

Set `serverSelectionTimeoutMS` on the `AsyncMongoClient` itself
(`MongoSettings.server_selection_timeout_ms`, default 5000ms), rather than only relying
on `asyncio.timeout()` at each call site. This bounds *every* operation the client ever
performs — `init_mongo`'s buildInfo ping, `check_mongo`'s ping, and anything a future
module does with this client — not just the ones we remembered to wrap individually.

`init_mongo`'s call in the lifespan is additionally wrapped in a try/except that logs a
warning and continues (defense in depth): even a fast, bounded failure here must not
abort startup.

## Consequences

- A Mongo outage at boot now costs at most ~`server_selection_timeout_ms` (5s default)
  before the app finishes starting, instead of 30s before crashing.
- Verified manually: starting the app with no Mongo/Postgres/Redis reachable completes
  in ~7s total (bounded by the slowest of the three checks) and logs one
  `dependency_unavailable_at_startup` warning per unreachable dependency;
  `/health/ready` then correctly returns 503 naming each one. Starting a real local
  Mongo mid-test flipped its check to healthy on the next request with no restart
  needed.
- Any future code that constructs its own Mongo client (rather than reusing
  `app.state.mongo_client`) must set this same option, or it silently reintroduces the
  30s hang for that one code path.
