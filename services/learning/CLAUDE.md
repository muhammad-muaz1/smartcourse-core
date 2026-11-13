# CLAUDE.md — learning service

Root `CLAUDE.md` governs architecture, data rules, async tooling, auth, response contracts,
observability and coding standards. This file covers only what is specific to this service.

## What this service owns

Enrollments, progress and completions; certificates as they are built. It is
authoritative for whether a student is enrolled, how far they have got, and whether they
finished. This is the highest-write-volume service and sits on the hottest path.

**Database:** `learning_db` (PostgreSQL). No other service reads it.
**Redis DB index:** `3` — `Idempotency-Key` responses for enrollment and completion.
**Port (local):** `8003`
**Processes:** the API (`python -m src.main`) and the worker (`python -m src.worker`:
Kafka consumer, group `learning`, + outbox relay).

## Modules

| Module | Responsibility |
| --- | --- |
| `enrollments` | the enrollment aggregate: enrol, withdraw, progress, completion, roster, history; the `course_cache` and `student_directory` read models |

## Events produced

All via `outbox_messages`, in the same transaction as the change; each carries the
enrollment's full snapshot.

| Event | Emitted when |
| --- | --- |
| `enrollment.created.v1` | a student enrols |
| `enrollment.withdrawn.v1` | an active enrollment is withdrawn (a repeat emits nothing) |
| `enrollment.completed.v1` | the last lesson is completed |

## Events consumed

| Event | From | What this service does |
| --- | --- | --- |
| `course.*.v1` | `course` | upsert `course_cache` — ignored unless `revision` is newer |
| `user.registered.v1` | `identity` | students' display names into `student_directory` |

Kafka consumer (`consumers.py`); the runner dedupes through the inbox (docs/adr/0012).

## Local read models

`course_cache` — course id, title, status, instructor, seat limit, lesson list, revision.
Enrollment and progress read it instead of calling `course`. It trails course by the
relay's and consumer's latency (about a second locally): a course published a moment ago
may briefly 404, or still read `publishing`, on enrol.

`active_enrollments` on the same row is learning's own counter, maintained under the row
lock in the enrollment transaction and never overwritten by an event.

`student_directory` — student id → display name, from `user.registered.v1`. Copied onto
the enrollment as a snapshot; rosters fall back to it if the event arrived after enrolment.

## Service-specific rules

- Enrollment is ONE transaction: `SELECT … FOR UPDATE` on the cache row, insert (progress
  initialised), seat check, counter increment, outbox row. Never split it, never add a
  network call to it.
- Duplicate prevention is the partial unique index on `(student_id, course_id) WHERE status
  <> 'withdrawn'`, never a `SELECT` first. Insert before the seat check, so an enrolled student
  retrying a full course hears `ALREADY_ENROLLED`, not `COURSE_FULL`.
- Lesson completion is an idempotent insert plus a recount under the enrollment's row lock;
  only the transition to `completed` emits an event. Completed and withdrawn are terminal.
- `POST /courses/{id}/enrollments` and `POST /enrollments/{id}/lessons/{lid}/complete`
  require an `Idempotency-Key` (middleware in `app.py`).
- Course status is compared as a string (`"published"`): course's enum is course's.
