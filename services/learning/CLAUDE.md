# CLAUDE.md — learning service

Root `CLAUDE.md` governs architecture, data rules, async tooling, auth, response contracts,
observability and coding standards. This file covers only what is specific to this service.

## What this service owns

Enrollments today; progress, completions and certificates as they are built. It is
authoritative for whether a student is enrolled. This is the highest-write-volume service
and sits on the hottest path.

**Database:** `learning_db` (PostgreSQL). No other service reads it.
**Task queue:** `learning-tq` (Temporal) — receives `course.*.v1` events.
**Redis DB index:** `3` — `Idempotency-Key` responses for enrollment.
**Port (local):** `8003`
**Processes:** the API (`python -m src.main`) and the worker (`python -m src.worker`).

## Modules

| Module | Responsibility |
| --- | --- |
| `enrollments` | enrol, withdraw, my enrollments, roster, history, seat and status enforcement; the `course_cache` read model |

## Events produced

None yet. `enrollment.created.v1` / `enrollment.withdrawn.v1` arrive with their first
consumer (notification, analytics) — with an outbox in this database, written in the
enrollment transaction.

## Events consumed

| Event | From | What this service does |
| --- | --- | --- |
| `course.*.v1` | `course` | upsert `course_cache` — inbox dedupe on `event_id`, ignored unless `revision` is newer |

Delivered today as the `learning.apply_course_event.v1` Temporal activity (course
schedules it by name; `smartcourse_contracts`), later as a Kafka consumer (docs/adr/0011).

## Local read models

`course_cache` — course id, title, status, instructor, seat limit, revision. Enrollment
reads it instead of calling `course`. It can trail course by the relay's latency (about a
second locally): a course published a moment ago may briefly 404 on enrol.

`active_enrollments` on the same row is learning's own counter, maintained under the row
lock in the enrollment transaction and never overwritten by an event.

## Service-specific rules

- Enrollment is ONE transaction: `SELECT … FOR UPDATE` on the cache row, insert, seat check,
  counter increment. Never split it, never add a network call to it.
- Duplicate prevention is the partial unique index on `(student_id, course_id) WHERE status
  = 'active'`, never a `SELECT` first. Insert before the seat check, so an enrolled student
  retrying a full course hears `ALREADY_ENROLLED`, not `COURSE_FULL`.
- `POST /courses/{id}/enrollments` requires an `Idempotency-Key` (middleware in `app.py`).
- Course status is compared as a string (`"published"`): course's enum is course's.
