# CLAUDE.md — course service

Root `CLAUDE.md` governs architecture, data rules, async tooling, auth, response contracts,
observability and coding standards. This file covers only what is specific to this service.

## What this service owns

The course catalogue and everything inside a course: courses, modules, lessons (metadata
**and** body), publish runs; prerequisites and versions as they are built. It is
authoritative for what a course contains and whether it is published, and it owns the
publishing saga.

**Database:** `course_db` (PostgreSQL). No other service reads it.
**Task queue:** `course-tq` (Temporal) — `PublishCourseWorkflow`.
**Redis DB index:** `1` — `Idempotency-Key` responses for publish; the catalogue cache later.
**Object storage:** lesson attachments — MinIO / S3-compatible (`make minio`), or local
files for dev. See **Asset storage** below.
**Port (local):** `8002`
**Processes:** the API (`python -m src.main`) and the worker (`python -m src.worker`:
Temporal worker + outbox relay). The API never talks to Temporal or Kafka. The API is the
only process that talks to object storage.

## Modules

| Module | Responsibility |
| --- | --- |
| `courses` | the whole course aggregate: catalogue, metadata, status lifecycle, modules, lessons, publish runs and the publish saga |
| `assets`  | uploaded file metadata (lesson attachments) + the storage backend; bytes live in object storage, not the database |

`courses` is one module on purpose: every write checks the course's ownership and status
under its row lock, and splitting modules/lessons/publishing out would mean modules
importing each other's models. Inside it, `service/` and `routes/` are split by concern
(courses, structure, publishing). `assets` is separate because it's a distinct concern
(binary storage) and touches courses/lessons only by id, through `courses`' service
helpers — it never imports the aggregate's write internals.

## Asset storage

Lesson attachments (PDF/DOC/PPT/XLS/TXT/MD/CSV/ZIP, ≤50 MB) upload to object storage; the
`assets` table holds only metadata (`storage_key`, size, sha256, owner), and is the
access-control anchor — a download is authorised against the asset's course, exactly like
a lesson (published → anyone; otherwise owner/admin only).

- `modules/assets/storage.py` is the seam: a `Storage` protocol (`put`/`get`/`delete`) with
  two backends — `MinioStorage` (aioboto3, S3-compatible: MinIO locally, any S3 endpoint in
  prod) and `LocalFilesystemStorage` (the default, so tests and a bare checkout need nothing
  running). Switching is one config value (`SMARTCOURSE_STORAGE__BACKEND`).
- Upload is a **raw-body** POST (`/assets?course_id=&filename=&lesson_id=`), not multipart,
  so the body streams straight to storage. It's gated by the same editable+ownership rules
  as any content write; the course row is locked first.
- Deleting a row schedules the byte-delete as a response background task — i.e. after the
  transaction commits — so a rollback never strands a row without its file. A failed/partial
  upload cleans up its bytes inline.
- Large videos stay URL-only (the 50 MB cap and the gateway's whole-body buffering rule
  them out of this path); the real fix is a presigned PUT straight to the bucket.

## Status lifecycle

`draft → publishing → published | failed`; `published → draft` (unpublish);
anything but `publishing → archived`. Content (modules, lessons) is editable only in
`draft`/`failed`. Metadata is editable in every state but `publishing`/`archived`.

## Events produced

All via `outbox_messages`, in the same transaction as the change, delivered by the relay —
to Kafka, except the internal publish-requested message (a Temporal start). Every
`course.*.v1` carries the full snapshot (`smartcourse_contracts.course_events`), including
the lesson list in reading order, and a `revision` that increases with every change.

| Event | Emitted when |
| --- | --- |
| `course.created.v1` | course created |
| `course.updated.v1` | metadata change; status → `publishing` / `failed` |
| `course.published.v1` | publish saga's last step |
| `course.unpublished.v1` | `POST /courses/{id}/unpublish` |
| `course.archived.v1` | `DELETE /courses/{id}` |
| `workflow.failed.v1` | the publish saga compensated (in `fail_publish`'s transaction) |
| `course.publish_requested.v1` | internal: the relay turns it into a `PublishCourseWorkflow` start |

Consumers: learning (`course_cache`), analytics (`course_facts`) — docs/adr/0012.

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
