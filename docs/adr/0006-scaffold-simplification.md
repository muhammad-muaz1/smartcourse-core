# 0006 — Scaffold simplified to a NestJS-style flat-module structure

- Status: Accepted
- Date: 2026-09-18

## Context

The original scaffold (Phases 0–2) built out `core/` and `db/` as multi-file
subpackages — nine sub-packages under `core/`, a generic `Repository`/`UnitOfWork`
pair, a Lua rate limiter, idempotency middleware, OTel tracing and Prometheus metrics,
and a from-scratch Mongo migration-runner framework — all before a single business
endpoint existed. Every piece was individually defensible against the original
CLAUDE.md, but the sum was a codebase where most files existed to serve requirements
that were still hypothetical, and a new developer had to open a dozen files to find
where one request is handled.

CLAUDE.md was rewritten to fix this directly, modelled on NestJS: one flat file per
concern in `core/`, one flat file per store in `db/`, one folder per feature module,
and an explicit rule that infrastructure isn't built until something imports it.

## Decision

Rebuilt the scaffold to match the new CLAUDE.md exactly:

- **Package rename**: `src/smartcourse/` → `src/` directly (imports are now
  `from src.core... import ...`, not `from smartcourse.core...`).
- **Removed** (no real caller existed for any of these): OpenTelemetry tracing +
  Prometheus metrics, the Lua sliding-window rate limiter, idempotency middleware,
  security-headers/GZip/TrustedHost/body-size-limit middleware, the generic
  `Repository`/`UnitOfWork` classes, and the Mongo migration-runner framework
  (`Migration` dataclass + loader + `_migrations`-collection runner).
- **Collapsed** `core/` from 9 sub-packages to 6 flat files (`router.py`,
  `response.py`, `exceptions.py`, `handlers.py`, `middleware.py`, `logging.py`) and
  `db/` from 3 subpackages (6+ files each) to 4 flat files (`base.py`, `postgres.py`,
  `mongo.py`, `redis.py`), each owning its own connection lifecycle as module-level
  state rather than being threaded through `app.state`.
- **Health collapsed** from three routes (`/health/live`, `/health/ready`,
  `/health/startup`) plus a shared `core/health.py` cache helper into one
  `GET /health` (owned by `modules/health/`, the health module's own `service.py`, not
  core — it has exactly one caller) plus `GET /version`.
- **Error shape simplified**: RFC 9457 Problem Details replaced with
  `{"code", "message", "details", "request_id"}` — no `type`/`title`/`status`/
  `instance`/`trace_id` fields, no `application/problem+json` media type.
- **Transaction model changed**: no `UnitOfWork` class — `get_session` is a FastAPI
  generator dependency that commits on clean exit and rolls back on exception, driven
  by FastAPI's own dependency teardown.

## A bug this surfaced: `create_app(settings)` was silently ignoring `settings`

While rewriting `app.py`, the lifespan function called `get_settings()` directly
instead of closing over the `settings` argument passed into `create_app()`. This meant
every test that constructed `create_app(custom_settings)` to point at a disposable test
database was actually still connecting to whatever `get_settings()` resolved from the
real environment — silently. It surfaced as a health-check test reporting Postgres and
Mongo as healthy when pointed at a deliberately unreachable port, because the app was
quietly using the developer's real local databases instead.

Fixed by restoring the closure pattern (`_build_lifespan(settings) -> lifespan`) so the
lifespan function actually captures the settings object it was given, matching the
pattern the Phase 1/2 scaffold already used correctly. Caught by
`tests/health/test_routes.py::test_health_returns_503_and_names_each_unhealthy_dependency`
before this ever shipped — the value of testing the unreachable-dependency path
explicitly, not just the happy path.

## What was kept unconditionally

- The store-ownership split (Postgres = invariants, Mongo = open-shaped/derived data) —
  a data-modelling decision, not scaffolding.
- The outbox + consumer-dedupe rule as a design constraint on the future write path,
  even though neither is built yet.
- Bounding every dependency client's own connect/selection timeout at the driver level
  (asyncpg `connect_args={"timeout": 5}`, Mongo `serverSelectionTimeoutMS`, Redis
  `socket_connect_timeout`) — the lesson from the original ADR 0005 applies regardless
  of how the surrounding scaffold is organized: an unreachable dependency must fail
  fast, not hang startup.

## Consequences

- Rate limiting, idempotency keys, and observability (tracing/metrics) are not gone —
  they're deferred to when a real endpoint needs them, per the new CLAUDE.md §6. Per
  the plan's own stated risk, these are exactly the kind of thing that tends to get
  noticed only after an incident; they should be tracked on the milestone plan with a
  date, not left to be "noticed in the moment."
- Auto-discovery (`core/router.py` walking `modules/*/routes.py`) survived the
  simplification unchanged — it was already justified by having a real, if singular,
  consumer (the health module) and scales the same way regardless of file count
  elsewhere.
