# CLAUDE.md — course service

Root `CLAUDE.md` governs architecture, data rules, async tooling, auth, response contracts,
observability and coding standards. This file covers only what is specific to this service.

## What this service owns

The course catalogue and everything inside a course: courses, modules, lessons (metadata
**and** body), publish runs; prerequisites and versions as they are built. It is
authoritative for what a course contains and whether it is published, and it owns the
publishing saga.

**Database:** `course_db` (PostgreSQL). No other service reads it.
**Task queue:** `course-tq` (Temporal) — `PublishCourseWorkflow`, `DeliverCourseEventWorkflow`.
**Redis DB index:** `1` — `Idempotency-Key` responses for publish; the catalogue cache later.
**Port (local):** `8002`
**Processes:** the API (`python -m src.main`) and the worker (`python -m src.worker`:
Temporal worker + outbox relay). The API never talks to Temporal.

## Modules

| Module | Responsibility |
| --- | --- |
| `courses` | the whole course aggregate: catalogue, metadata, status lifecycle, modules, lessons, publish runs and the publish saga |

One module on purpose: every write checks the course's ownership and status under its row
lock, and splitting modules/lessons/publishing out would mean modules importing each
other's models. Inside it, `service/` and `routes/` are split by concern (courses,
structure, publishing).

## Status lifecycle

`draft → publishing → published | failed`; `published → draft` (unpublish);
anything but `publishing → archived`. Content (modules, lessons) is editable only in
`draft`/`failed`. Metadata is editable in every state but `publishing`/`archived`.

## Events produced

All via `outbox_messages`, in the same transaction as the change, delivered by the relay.
Every `course.*.v1` carries the full snapshot (`smartcourse_contracts.course_events`) and a
`revision` that increases with every change.

| Event | Emitted when |
| --- | --- |
| `course.created.v1` | course created |
| `course.updated.v1` | metadata change; status → `publishing` / `failed` |
| `course.published.v1` | publish saga's last step |
| `course.unpublished.v1` | `POST /courses/{id}/unpublish` |
| `course.archived.v1` | `DELETE /courses/{id}` |
| `course.publish_requested.v1` | internal: the relay turns it into a `PublishCourseWorkflow` start |

Until Kafka exists, the relay delivers `course.*.v1` to learning by starting
`DeliverCourseEventWorkflow`, which runs learning's `learning.apply_course_event.v1`
activity on `learning-tq` (docs/adr/0011).

## Events consumed

None yet. `content.indexed.v1` arrives with the content service's publish steps.

## Service-specific rules

- `instructor_id` is an identity-service user id: a plain UUID, **no foreign key**. Never
  look a user up — if a view needs a name, store a snapshot from an event.
- Ownership (owning instructor or admin) is checked in the service, never inline in routes.
- Every write to the aggregate locks the course row first (`get_course_for_update`) — it
  serialises writers so positions and `revision` never collide.
- Every change learning cares about goes through `record_course_event` — never write a
  status or seat limit without it, or `course_cache` silently drifts.
- Workflow-side service functions (`validate_for_publish`, `complete_publish`,
  `fail_publish`) must stay safe to run twice: activities are at-least-once.
- The role enum is identity's. Compare the JWT's role claim as a string (`"admin"`).
- Tokens are verified offline with identity's public key. No call to identity, ever.
