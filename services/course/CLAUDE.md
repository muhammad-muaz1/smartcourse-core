# CLAUDE.md — course service

Root `CLAUDE.md` governs architecture, data rules, async tooling, auth, response contracts,
observability and coding standards. This file covers only what is specific to this service.

## What this service owns

The course catalogue and everything inside a course: courses today; modules, lessons
(metadata **and** body), prerequisites, versions and publish runs as they are built. It
is authoritative for what a course contains and whether it is published, and it owns
the publishing saga.

**Database:** `course_db` (PostgreSQL). No other service reads it.
**Task queue:** `course-tq` (Temporal) once publishing lands — none yet.
**Redis DB index:** `1`, reserved for the catalogue cache. Not connected yet: nothing
here uses Redis.
**Port (local):** `8002`

## Modules

| Module | Responsibility |
| --- | --- |
| `courses` | create, browse (`?q=&level=&status=`, cursor pagination), read, update metadata, archive |

## Events produced

None yet. `course.created.v1` / `course.updated.v1` / `course.archived.v1` arrive with the
outbox (API design §12) — `learning` needs them for its `course_cache`.

## Events consumed

None yet. `content.indexed.v1` arrives with the publishing workflow.

## Service-specific rules

- `instructor_id` is an identity-service user id: a plain UUID, **no foreign key**. Never
  look a user up — if a view needs a name, store a snapshot from an event.
- Ownership (owning instructor or admin) is checked in `service.py`, never inline in routes.
- The role enum is identity's. Compare the JWT's role claim as a string (`"admin"`); never
  import or copy identity's `UserRole`.
- Tokens are verified offline with identity's public key. No call to identity, ever.
