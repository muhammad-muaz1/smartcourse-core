# CLAUDE.md — analytics service

Root `CLAUDE.md` governs architecture, data rules, async tooling, auth, response contracts,
observability and coding standards. This file covers only what is specific to this service.

## What this service owns

Projections built from every event on the platform, and every reported metric (the
brief's nine). It is authoritative for nothing — every number is derived, eventually
consistent, and rebuildable from the retained Kafka log.

**Database:** `analytics_db` (PostgreSQL). No other service reads it.
**Redis DB index:** `5` — metric responses, cached for the API design §10 TTLs.
**Port (local):** `8005`
**Processes:** the API (`python -m src.main`) and the worker (`python -m src.worker`:
one Kafka consumer, group `analytics`, over every registered topic).

## Modules

| Module | Responsibility |
| --- | --- |
| `metrics` | fact tables, projection appliers, consumers, metric queries, `/analytics/*` |

## Events produced

None.

## Events consumed

Every topic in `smartcourse_contracts.registry` — a test fails if one is missing.

| Event | Projection |
| --- | --- |
| `user.registered.v1` | `user_facts` (role only — no name, no email) |
| `course.*.v1` | `course_facts` |
| `enrollment.*.v1` | `enrollment_facts` |
| `workflow.failed.v1`, dead letters | `failure_records` |

## Service-specific rules

- Facts, never counters. Every projection is an upsert keyed by the entity's id and
  guarded by `as_of`; every metric is a query. A counter can't survive a replay.
- Every response carries `meta.as_of`. Never present a projection as real-time.
- Platform-wide figures are admin-only; an instructor sees their own courses, judged by
  `course_facts.instructor_id` — no call to course.
- Rebuild = delete the projection rows, reset the `analytics` group to earliest, restart
  the worker. Event topics are retained forever for exactly this.
