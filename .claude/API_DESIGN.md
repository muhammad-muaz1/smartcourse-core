# SmartCourse — API Design (Part A)

Target location: `docs/architecture/api-design.md`

**Current state:** scaffold is running — Postgres, MongoDB and Redis connected, health check
working. Kafka, RabbitMQ/Celery and Temporal are **not** wired yet. This design marks exactly
which endpoints are affected by that and how each one changes when they arrive.

---

## 1. How to read the tables

**Type** — how the endpoint executes:

| Tag | Meaning |
| --- | --- |
| **Simple** | Request → one transaction → response. Nothing in the background. Final shape; will not change. |
| **Simple → Event** | Simple today. When Kafka lands it also writes an outbox row. **The response contract does not change** — only work moves off the request path. |
| **Event** | Genuinely asynchronous: returns `202` + a status resource. Needs Temporal or Celery, so it is built in an interim synchronous form today (noted per endpoint). |
| **Projection read** | Read-only aggregate. Computed live from Postgres today; served from a Mongo projection once consumers exist. Response contract identical either way. |

**Store / ORM**

| Store | Access layer | Used for |
| --- | --- | --- |
| PostgreSQL | **SQLAlchemy 2.0 async** (`asyncpg`), Alembic migrations | Anything with an invariant at commit time |
| MongoDB | **Beanie** (Pydantic v2 documents) | Open-shaped and derived data |
| Redis | **`redis.asyncio`** | Cache, rate limits, locks, counters — nothing durable |

**Roles:** `—` public · `S` student · `I` instructor · `A` admin · `owner` = the instructor who
owns the course (ownership check in the module's service, not a role check).

---

## 2. Module → store map

| Module | Primary store | Also touches |
| --- | --- | --- |
| `auth` | Postgres (SQLAlchemy) | Redis (login throttle, reset tokens) |
| `users` | Postgres (SQLAlchemy) | — |
| `courses` | Postgres (SQLAlchemy) | Mongo (lesson bodies, chunks), Redis (catalogue cache) |
| `enrollments` | Postgres (SQLAlchemy) | Mongo (history) |
| `progress` | Postgres (SQLAlchemy) | — |
| `notifications` | Mongo (Beanie) | Postgres (preferences), Redis (unread count) |
| `analytics` | Postgres today → Mongo projections later | Redis (response cache) |

---

## 3. Auth

| Endpoint | Purpose | Roles | Type | Store / ORM |
| --- | --- | --- | --- | --- |
| `POST /auth/register` | Create account | — | **Simple → Event** | Postgres / SQLAlchemy · Redis (IP throttle) |
| `POST /auth/login` | Issue token pair | — | Simple | Postgres / SQLAlchemy · Redis (failed-attempt counter) |
| `POST /auth/refresh` | Rotate token pair | — | Simple | Postgres / SQLAlchemy |
| `POST /auth/logout` | Revoke token family | S I A | Simple | Postgres / SQLAlchemy · Redis (deny-list) |
| `GET /auth/me` | Current user + permissions | S I A | Simple | Postgres / SQLAlchemy · Redis (60s cache) |
| `POST /auth/password/forgot` | Request reset | — | **Simple → Event** | Redis (single-use token) |
| `POST /auth/password/reset` | Set new password | — | Simple | Postgres / SQLAlchemy — revokes all families |
| `GET /.well-known/jwks.json` | Public signing keys | — | Simple | in-memory from settings |

Register and forgot-password both need to send mail. Until Celery exists, the service calls a
`NotificationSender` interface whose only implementation logs the message. When Celery lands,
swap the implementation — no route or service change.

---

## 4. Users

| Endpoint | Purpose | Roles | Type | Store / ORM |
| --- | --- | --- | --- | --- |
| `GET /users` | List users | A | Simple | Postgres / SQLAlchemy |
| `GET /users/{id}` | Detail | A, self | Simple | Postgres / SQLAlchemy |
| `PATCH /users/{id}` | Update profile | A, self | Simple | Postgres / SQLAlchemy |
| `PATCH /users/{id}/role` | Change role | A | **Simple → Event** | Postgres / SQLAlchemy |

---

## 5. Courses, modules, lessons

| Endpoint | Purpose | Roles | Type | Store / ORM |
| --- | --- | --- | --- | --- |
| `POST /courses` | Create draft | I | Simple | Postgres / SQLAlchemy |
| `GET /courses` | Browse catalogue (`?q=&level=&status=`) | — | Simple | Postgres FTS / SQLAlchemy · Redis 60s |
| `GET /courses/{id}` | Detail + module tree | — | Simple | Postgres / SQLAlchemy · Mongo / Beanie |
| `PATCH /courses/{id}` | Update metadata | owner, A | Simple | Postgres / SQLAlchemy |
| `DELETE /courses/{id}` | Archive (soft) | owner, A | **Simple → Event** | Postgres / SQLAlchemy |
| `GET /courses/{id}/prerequisites` | List | — | Simple | Postgres / SQLAlchemy |
| `PUT /courses/{id}/prerequisites` | Replace set | owner, A | Simple | Postgres / SQLAlchemy (cycle check in one txn) |
| `POST /courses/{id}/modules` | Add module | owner | Simple | Postgres / SQLAlchemy |
| `GET /courses/{id}/modules` | List modules | — | Simple | Postgres / SQLAlchemy |
| `PATCH /modules/{id}` | Update | owner | Simple | Postgres / SQLAlchemy |
| `DELETE /modules/{id}` | Remove | owner | Simple | Postgres / SQLAlchemy |
| `PUT /courses/{id}/modules/order` | Reorder | owner | Simple | Postgres / SQLAlchemy (one txn) |
| `POST /modules/{id}/lessons` | Add lesson | owner | Simple | Postgres (metadata) · Mongo (body) |
| `GET /lessons/{id}` | Lesson detail | enrolled S, owner, A | Simple | Postgres (access check) · Mongo (body) |
| `PATCH /lessons/{id}` | Update | owner | Simple | Postgres · Mongo |
| `DELETE /lessons/{id}` | Remove | owner | Simple | Postgres · Mongo |

**Why lessons span two stores.** Ordering, access control and parent-child relationships are
invariants — Postgres. The body is a video ref, PDF, quiz spec or markdown, and its shape changes
every quarter — Mongo. The lesson row holds `content_ref` and `content_version`; the Beanie
document holds the payload.

**Assets are deferred.** Upload endpoints need object storage, which the stack doesn't include.
Recommendation is MinIO; until that's agreed, don't build asset routes. Don't work around it with
GridFS — it's hard to unpick later.

---

## 6. Publishing — the one genuinely event-driven flow

| Endpoint | Purpose | Roles | Type | Store / ORM |
| --- | --- | --- | --- | --- |
| `POST /courses/{id}/publish` | Start publish | owner, A | **Event** | Postgres (status) · Mongo (run record + chunks) · Temporal later |
| `GET /courses/{id}/publish-runs` | Run history | owner, A | Simple | Mongo / Beanie |
| `GET /courses/{id}/publish-runs/{run_id}` | Run status per step | owner, A | Simple | Mongo / Beanie |
| `POST /courses/{id}/publish-runs/{run_id}/retry` | Retry failed run | A | **Event** | Temporal later |
| `POST /courses/{id}/unpublish` | Withdraw | owner, A | **Simple → Event** | Postgres / SQLAlchemy |

Steps: `validate → extract → chunk → persist chunks (Mongo) → index → mark ready`.
`course.status`: `draft → publishing → ready`, or `failed` with the failing step recorded. A
course appears in the catalogue only at `ready`.

**Build it now like this:** write each step as a separate function in
`modules/courses/publishing.py`, and have the service call them in sequence inside one request,
writing a `publish_run` document in Mongo as it goes. Return `202` with the run id **from day
one**, even though the work finished before the response — because the response contract must not
change when Temporal arrives. At that point each function becomes an activity and the sequence
becomes the workflow. Nothing above the service layer moves.

This is the seam for the future AI service: it will consume `course.published` and embed the
chunks you're already writing.

---

## 7. Enrollment

| Endpoint | Purpose | Roles | Type | Store / ORM |
| --- | --- | --- | --- | --- |
| `POST /courses/{id}/enrollments` | Enrol | S | **Simple → Event** | Postgres / SQLAlchemy (one txn) |
| `GET /enrollments/me` | My enrollments | S | Simple | Postgres / SQLAlchemy |
| `GET /enrollments/{id}` | Detail | S (own), owner, A | Simple | Postgres / SQLAlchemy |
| `DELETE /enrollments/{id}` | Withdraw | S (own), A | **Simple → Event** | Postgres / SQLAlchemy |
| `GET /courses/{id}/enrollments` | Roster | owner, A | Simple | Postgres / SQLAlchemy |
| `GET /users/{id}/enrollment-history` | Full history incl. withdrawals | S (self), A | Simple | Mongo / Beanie |
| `POST /courses/{id}/enrollments/bulk` | Corporate batch | A | **Event** | Temporal later — do not build yet |

Enrolment stays one Postgres transaction even after Kafka arrives. It is the hottest path in the
system and orchestrator latency does not belong on it. The three rules from the brief:

| Rule | Enforcement |
| --- | --- |
| No duplicate enrollment | Unique constraint on `(student_id, course_id)` where not withdrawn |
| Seat limit | `SELECT … FOR UPDATE` on the course row in the same transaction as the insert |
| Prerequisites | Query against completed enrollments in the same transaction |

Today, progress initialisation happens **inline in that same transaction** — correct and simple.
When Kafka lands it moves to a consumer of `enrollment.created`, and the endpoint gains an outbox
write. Analytics and the welcome notification follow the same path.

---

## 8. Progress & certificates

| Endpoint | Purpose | Roles | Type | Store / ORM |
| --- | --- | --- | --- | --- |
| `GET /enrollments/{id}/progress` | Progress detail | S (own), owner, A | Simple | Postgres / SQLAlchemy |
| `PUT /enrollments/{id}/lessons/{lid}/progress` | Upsert position | S (own) | Simple | Postgres / SQLAlchemy (idempotent upsert) |
| `POST /enrollments/{id}/lessons/{lid}/complete` | Mark complete | S (own) | **Simple → Event** | Postgres / SQLAlchemy |
| `GET /courses/{id}/progress-summary` | Cohort progress | owner, A | **Projection read** | Postgres today → Mongo later · Redis 5m |
| `GET /enrollments/{id}/certificate` | Fetch certificate | S (own), A | Simple | Postgres / SQLAlchemy |
| `GET /certificates/{code}` | Public verification | — | Simple | Postgres / SQLAlchemy · Redis cache |

Progress heartbeats are the highest-frequency write here — a video player posting every few
seconds. Idempotent upsert to Postgres, **no event ever**. Only completion emits.

Certificates: today, issue the record row synchronously when the last lesson completes. The PDF
render becomes a Celery task consuming `course.completed` later. Until then
`GET .../certificate` returns the record without a file URL.

---

## 9. Notifications

| Endpoint | Purpose | Roles | Type | Store / ORM |
| --- | --- | --- | --- | --- |
| `GET /notifications/me` | Inbox (`?unread=true`) | S I A | Simple | Mongo / Beanie · Redis (unread count) |
| `POST /notifications/{id}/read` | Mark read | record owner | Simple | Mongo / Beanie · Redis |
| `POST /notifications/read-all` | Mark all read | record owner | Simple | Mongo / Beanie · Redis |
| `GET /notifications/preferences` | Channel preferences | S I A | Simple | Postgres / SQLAlchemy |
| `PUT /notifications/preferences` | Update | S I A | Simple | Postgres / SQLAlchemy |

**No send endpoint, ever.** Notification records are written by services today and by event
consumers later. An API that lets a client send a notification is an abuse vector.

---

## 10. Analytics

Every endpoint here is a **Projection read**. All eight brief metrics are covered.

| Endpoint | Metric from the brief | Roles | Store / ORM |
| --- | --- | --- | --- |
| `GET /analytics/overview` | Total students, instructors, courses published | A, I | Postgres aggregate today → Mongo projection · Redis 60s |
| `GET /analytics/enrollments?from=&to=&granularity=` | New enrollments over time | A, owner | same · Redis 5m |
| `GET /analytics/completion?course_id=` | Course completion rate | A, owner | same · Redis 5m |
| `GET /analytics/time-to-complete?course_id=` | Average time to complete | A, owner | same · Redis 5m |
| `GET /analytics/courses/popular?period=` | Most popular courses | A I S | same · Redis 5m |
| `GET /analytics/students/course-load` | Average courses per student | A | same · Redis 15m |
| `GET /analytics/courses/{id}` | Per-course instructor dashboard | owner, A | same · Redis 5m |
| `GET /analytics/failures` | Failed events / workflow issues | A | **Not buildable yet** — needs a DLQ |

**Be explicit with the client about the interim.** Without Kafka there are no projections, so
these are computed live from Postgres with a Redis cache. That is fine at current data volume and
will not survive real load — grouping every enrollment row on every dashboard refresh is exactly
the contention the brief complains about.

Two things make the swap painless. Keep every query behind
`modules/analytics/service.py`, never in a route. And return `meta.as_of` in every response from
day one, so clients are already handling staleness before the data is actually stale.

The failures endpoint depends on a DLQ, which depends on consumers. Don't stub it with fake data.

---

## 11. Operations

| Endpoint | Purpose | Roles | Type |
| --- | --- | --- | --- |
| `GET /health` | Overall + per-dependency status | — | Simple (built) |
| `GET /version` | Git SHA, build time | — | Simple |
| `GET /metrics` | Prometheus scrape | internal | Simple |
| `GET /admin/dlq` · `POST /admin/dlq/{id}/replay` | Inspect and replay dead letters | A | Deferred until Kafka |
| `GET /admin/outbox/stats` | Backlog size and oldest unpublished age | A | Deferred until the outbox exists |

---

## 12. What becomes an event, and when

Everything tagged **Simple → Event** above emits exactly one event, written to the outbox in the
**same transaction** as the state change:

| Endpoint | Event | Consumers |
| --- | --- | --- |
| `POST /auth/register` | `user.registered.v1` | notifications, analytics |
| `PATCH /users/{id}/role` | `user.role_changed.v1` | analytics |
| publish workflow terminal step | `course.published.v1` | analytics, notifications, *(future AI indexing)* |
| `POST /courses/{id}/unpublish` | `course.unpublished.v1` | analytics, cache invalidation |
| `DELETE /courses/{id}` | `course.archived.v1` | analytics |
| `POST /courses/{id}/enrollments` | `enrollment.created.v1` | progress init, analytics, notifications |
| `DELETE /enrollments/{id}` | `enrollment.withdrawn.v1` | analytics |
| `POST .../lessons/{id}/complete` | `progress.updated.v1`, and `course.completed.v1` when it's the last lesson | analytics; certificate issuance, notifications |

Topics: `smartcourse.<domain>.<event>.v<major>`, partition key = aggregate id.

### Three rules to follow while events don't exist yet

**Services take a session, they don't fetch one.** Every service function's first parameter is
the session. A consumer processing `enrollment.created` has no HTTP request, so a service that
acquires its own session is unusable from anywhere but a route. This one signature decision is
what makes the same service callable from a route, a task, a consumer or an activity unchanged.

**Side effects go behind an interface.** Sending mail, initialising progress, bumping a counter —
call a named function, not inline code in the endpoint. When Kafka arrives, the endpoint writes
an outbox row and the consumer calls that same function.

**Return `202` now for anything that will be asynchronous later.** Publish and bulk enrol. Adding
a status resource after clients depend on a `200` body is a breaking change.

---

## 13. Open questions for the client

1. **Object storage** — MinIO recommended, not in the stated stack. Asset endpoints are blocked
   on this.
2. **Enrollment approval** — self-service assumed. If corporate customers need manager approval,
   enrollment gains a `pending` state; cheap now, expensive after the state machine ships.
3. **Content versioning** — when a published course is edited, do enrolled students see changes
   immediately, or stay pinned to the version they enrolled on? This design assumes immediate,
   with `content_version` recorded so pinning stays possible.
4. **Certificate verification** — assumed public via code. Confirm.