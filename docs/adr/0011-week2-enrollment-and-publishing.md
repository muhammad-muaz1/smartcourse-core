# 0011 — Week 2: enrollment, content structure, and the publishing saga

- Status: Accepted
- Date: 2026-11-11

## Scope

Week-2 deliverables: enrollment with duplicate handling and validations, the course
publishing workflow on Temporal, modules/lessons, and course state transitions.

Built:

- **course**: modules and lessons (jsonb body + per-type `CHECK`), reorder, the status
  lifecycle (`draft → publishing → published | failed`, unpublish, archive), publish runs,
  `PublishCourseWorkflow`, a transactional outbox, and a worker process (Temporal worker +
  outbox relay).
- **learning** (new service, :8003, `learning_db`): `course_cache` read model, enrol,
  withdraw, my enrollments, roster, enrollment history.
- **libs/contracts** (new): `CourseEventV1` — the first cross-service contract.
- **platform-core**: `Idempotency-Key` middleware, class-based error → status mapping,
  `session_scope()`, `discover_attribute()`.
- **gateway**: learning upstream; pattern routes for learning's paths nested under
  `/courses` and `/users`; `/modules` and `/lessons` to course.

## Decisions

**Course → learning travels outbox → relay → Temporal activity, not Kafka (yet).**
The roadmap puts the Kafka event spine before learning; week 2's scope names Temporal and
not Kafka, and learning cannot enforce "published only" or seat limits without course's
data. The architecture allows exactly two cross-service mechanisms — events and Temporal
activities on the other service's queue — so the relay starts a one-activity
`DeliverCourseEventWorkflow` per event, scheduling `learning.apply_course_event.v1` on
`learning-tq` by name. What is permanent is the part that can't be retrofitted: the
outbox row in the same transaction, event-carried state with a monotonic `revision`, and
inbox dedupe on the consumer. When Kafka lands, only the relay's dispatchers and learning's
entry point change; the API, outbox, payload and `course_cache` logic do not.

Costs accepted: no fan-out (one consumer) and no replay from a log — both arrive with
Kafka. Temporal does give durable, retried delivery: if learning is down, delivery
retries until it returns.

**Contracts are Pydantic/JSON, not Avro.** There is no Schema Registry yet. The model
lives in `libs/contracts`, versioned in the name (`V1`), with Avro's compatibility rule
applied by hand: within v1, only add optional fields.

**The API never talks to Temporal.** `POST /publish` writes the run and a
`course.publish_requested.v1` outbox row in one transaction; the relay starts the
workflow (workflow id = `publish-run-{run_id}`, `REJECT_DUPLICATE`). A Temporal outage
delays publishing; it never fails or half-applies a request.

**Publish steps today are `validate → mark_published`.** Extract, chunk and index belong to
the content service, which doesn't exist; they will be activities on `content-tq`
inserted between the two, with their compensations (delete partial chunks) before
`mark_failed`. Not faked here.

**Validation runs in the saga, not the request.** An empty course is accepted (202) and
its run fails at `validate` with reasons ("course has no modules", "module 'X' has no
lessons"). That is the documented status-resource contract, and it keeps the request path
free of a check that would have to be repeated in the workflow anyway.

**No content versioning yet.** API design §4 resolves "edit a published course" with
versions. Until those exist: content is editable only in `draft`/`failed`; a published
course must be unpublished (→ `draft`) to change, which removes it from new enrollment.
Metadata (title, seats) stays editable while published and syncs via `course.updated.v1`.

**Insert before the seat check.** A student already enrolled in a full course who retries
must hear `ALREADY_ENROLLED`, not `COURSE_FULL` — the first version had it the other way
round and the end-to-end test caught it. The duplicate rule is still the unique index;
raising `COURSE_FULL` after the insert rolls the insert back.

**`CHECK` on jsonb uses `IS NOT DISTINCT FROM`.** `jsonb_typeof(body -> 'video_url') =
'string'` is NULL when the key is missing, and Postgres accepts a CHECK that evaluates to
NULL — the first version let `{}` through as a video body. Caught by a test that inserts
around the API.

**Idempotency is middleware in platform-core.** Second use (publish, enroll), so it moved
up. It must store the response after the route's transaction commits, which a dependency
cannot do. Keys are namespaced by the *verified* token subject, so a forged token can't
read someone else's stored response. Fails open if Redis is down: the database invariants
remain the backstop.

**Error codes are now specific.** `_STATUS_BY_CODE` became a class → status map resolved
along the MRO, so `class AlreadyEnrolledError(ConflictError): code = "ALREADY_ENROLLED"`
keeps 409. Side effect: a missing course is now `COURSE_NOT_FOUND` (was `NOT_FOUND`) —
same 404.

## Not done in this change

- **Kafka + Schema Registry, `enrollment.*` events, learning's outbox.** No consumer yet.
- **Progress initialisation on enroll.** The brief lists it under enrollment; the progress
  module is its own slice.
- **Prerequisites.** Need completion records in learning first.
- **Lesson access for "enrolled students only".** course has no enrollment data; today any
  authenticated user can read lessons of a published course. Needs `enrollment.*` events
  feeding course, or the check moving to learning.
- **Rate limiting** on enroll/publish (API design §13), **retry/cancel run** endpoints,
  **bulk enrollment**, **roster name snapshots** (needs `user.registered.v1`).
- **Running the workflow tests and compose stack here.** `tests/courses/test_workflows.py`
  (marked `temporal`) needs Temporal's test server, which temporalio downloads on first
  use; the compose file's new services are untested in Docker.

## Consequences

- Local dev needs a Temporal server (`make temporal`) and the two workers (`make dev` starts
  them; `make worker s=course|learning` individually). Without them, publish runs stay
  `pending` and learning never sees courses — the API still works.
- `GET /courses/{id}` now returns the module tree (`modules: [...]`), an additive change.
- Every Dockerfile copies `libs/contracts` and `services/learning`'s pyproject, because
  `uv sync --frozen` resolves the whole workspace.
