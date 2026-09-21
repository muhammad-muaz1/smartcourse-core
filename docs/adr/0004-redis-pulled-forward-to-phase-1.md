# 0004 — Redis client + rate limiter pulled forward into Phase 1

- Status: **Superseded by [0006](0006-scaffold-simplification.md)** (2026-09-18) — the
  Lua rate limiter and idempotency middleware described here were removed; neither had
  a real endpoint to protect yet. Kept for the historical record of why they were built
  when they were. Add them back, when a real endpoint needs them, with a fresh ADR
  rather than reviving this one.
- Date: 2026-09-17

## Context

The build plan puts `db/redis/` (client, cache helper, distributed lock, Lua
sliding-window rate limiter) in Phase 2, and the full middleware stack — including
`RateLimitMiddleware` (Redis sliding window via Lua) and `IdempotencyMiddleware`
(Redis-backed) — in Phase 1, per CLAUDE.md §7. Those two phase boundaries conflict:
Phase 1's acceptance criteria requires the exact §7 middleware order wired into
`app_factory`, but two of those nine middlewares have no dependency to call without a
Redis client.

## Decision

Build `db/redis/client.py` (connection factory) and `db/redis/rate_limiter.py` (the Lua
sliding-window script) in Phase 1, ahead of schedule, since they're required for the
middleware order to be real rather than stubbed. `db/redis/cache.py` (generic cache
helper) and `db/redis/locks.py` (distributed lock) are **not** pulled forward — nothing
in Phase 1 needs them, and building them now would be speculative.

This is implementation-order only, not a scope change: these files live at their
CLAUDE.md §3 path (`db/redis/`) and Phase 2 does not redo them, only adds the
Postgres/Mongo counterparts and the two Redis pieces above alongside a `/health/ready`
check that pings this same client.

## A related problem this surfaced: liveness must not depend on Redis

Wiring a real Redis-backed `RateLimitMiddleware` into the global stack means *every*
request needs Redis to be reachable — including `/health/live`, which CLAUDE.md §9
requires to stay up through a dependency outage ("a liveness probe that fails on a
dependency outage restarts healthy containers and makes the outage worse"). Verified by
running the app with no Redis listening: any non-excluded route failed with a Redis
`ConnectionError` surfaced as a 500, while an excluded route succeeded normally.

`RateLimitMiddleware` therefore takes an `excluded_paths` set, and `app_factory`
excludes both `/health/live` and `/metrics` — the latter because Prometheus scraping is
exactly the signal you need working *during* a Redis outage to diagnose it. No other
route is excluded; rate limiting fails closed (a Redis outage blocks business traffic
rather than silently letting it through unlimited) since CLAUDE.md treats backpressure
as a required control, not a best-effort one.

## Consequences

- Phase 2's Redis section is now "add cache.py and locks.py, plus a `/health/ready`
  check that reuses `db.redis.client`" rather than starting from nothing.
- If a future middleware or route legitimately must survive a Redis outage, add it to
  `app_factory._RATE_LIMIT_EXCLUDED_PATHS` deliberately — don't disable rate limiting
  globally to work around a single route's requirement.
