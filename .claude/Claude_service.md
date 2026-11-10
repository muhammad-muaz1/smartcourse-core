# Per-service `CLAUDE.md` template

Drop one of these at `services/<name>/CLAUDE.md`. It stays short on purpose — the root
`CLAUDE.md` holds everything shared, and duplicating it here means two files drifting apart.

Anything not stated below is governed by the root file.

---

## Template

```markdown
# CLAUDE.md — <service> service

Root `CLAUDE.md` governs architecture, data rules, async tooling, auth, response contracts,
observability and coding standards. This file covers only what is specific to this service.

## What this service owns

<one paragraph: the aggregate this service is authoritative for>

**Database:** `<name>_db` (PostgreSQL). No other service reads it.
**Task queue:** `<name>-tq` (Temporal), if this service runs a worker.
**Redis DB index:** `<n>`

## Modules

| Module | Responsibility |
| --- | --- |
| `<module>` | <one line> |

## Events produced

| Event | Emitted when | Notes |
| --- | --- | --- |
| `<domain>.<event>.v1` | <trigger> | outbox row in the same transaction |

## Events consumed

| Event | From | What this service does |
| --- | --- | --- |
| `<domain>.<event>.v1` | `<service>` | <effect> — deduped via inbox |

## Local read models

<tables this service maintains from other services' events, and which events feed them.
Delete this section if there are none.>

## Service-specific rules

<only genuinely local rules — invariants, hot-path constraints, things a newcomer would
get wrong. Three to six lines. If a rule applies to more than this service, it belongs
in the root file instead.>
```

---

## Worked example — `services/learning/CLAUDE.md`

```markdown
# CLAUDE.md — learning service

Root `CLAUDE.md` governs architecture, data rules, async tooling, auth, response contracts,
observability and coding standards. This file covers only what is specific to this service.

## What this service owns

Enrollments, progress, completions and certificates. It is authoritative for whether a student
is enrolled, how far they have got, and whether they finished. This is the highest-write-volume
service in the platform and sits on the hottest path.

**Database:** `learning_db` (PostgreSQL). No other service reads it.
**Task queue:** `learning-tq` (bulk enrollment workflow only)
**Redis DB index:** `3`

## Modules

| Module | Responsibility |
| --- | --- |
| `enrollments` | enrol, withdraw, roster, history, seat and prerequisite enforcement |
| `progress` | lesson progress, completion, certificates |

## Events produced

| Event | Emitted when | Notes |
| --- | --- | --- |
| `enrollment.created.v1` | a student enrols | outbox row in the enrollment transaction |
| `enrollment.withdrawn.v1` | a student withdraws | |
| `progress.updated.v1` | a lesson is completed | heartbeats emit nothing |
| `course.completed.v1` | the last lesson completes | triggers certificate + notification |

## Events consumed

| Event | From | What this service does |
| --- | --- | --- |
| `course.published.v1` | `course` | upsert `course_cache` |
| `course.updated.v1` | `course` | update capacity and prerequisites in `course_cache` |
| `course.unpublished.v1` / `course.archived.v1` | `course` | mark cache row unavailable |
| `user.registered.v1` | `identity` | store name snapshot for rosters |

## Local read models

`course_cache` — course id, status, capacity, prerequisites. Fed by `course.*` events.
Enrollment reads it instead of calling `course`, because a synchronous call on this path would
couple uptime and add latency to the busiest endpoint in the system.

It can be seconds stale, so one enrollment may exceed capacity during a burst. That is accepted
behaviour, not a bug — see the open question on waitlists. A nightly reconciliation job compares
checksums with `course` and alerts on divergence.

## Service-specific rules

- Enrollment is ONE transaction: prerequisite check, `SELECT … FOR UPDATE` on the capacity row,
  unique-constraint insert, progress initialisation, outbox row. Never split it.
- Duplicate prevention is the unique constraint on `(student_id, course_id) WHERE NOT withdrawn`,
  never an application-level `SELECT` first.
- Progress heartbeats are an idempotent upsert and emit NO event. A video player posts every few
  seconds; emitting would flood Kafka with data nobody consumes. Only completion emits.
- `POST /enrollments` and lesson completion require an `Idempotency-Key`.
- Certificates are issued by a Celery task consuming `course.completed`. PDF rendering must never
  run inside the completion request.
```

---

## Why this file is short

The root `CLAUDE.md` is read on every session in every service. If each service's file repeats
the data rules, the async rules and the coding standards, they drift within a month and nobody
can tell which copy is authoritative.

The test for putting a rule here rather than in the root: **would it be wrong in another
service?** "Enrollment is one transaction" is specific to `learning`. "Services take a session,
they never fetch one" applies everywhere, so it lives in the root file.