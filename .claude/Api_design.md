# SmartCourse — API Design

Target location: `docs/architecture/api-design.md`

Six services, PostgreSQL only, one database per service. Supersedes the monolith design.

---

## 1. How to read the tables

**Owner** — the service that handles the request and owns the data. Clients reach every service
through the gateway at `/api/v1`; service boundaries are invisible to clients.

**Type** — how the endpoint executes:

| Tag | Meaning |
| --- | --- |
| **Simple** | Request → one transaction in the owning service's database → response. Final shape. |
| **Simple + Event** | One transaction, plus an outbox row written inside it. The response contract is identical to Simple. |
| **Event** | Asynchronous: `202` + a status resource. Temporal workflow underneath. |
| **Projection** | Read from a projection built by event consumers. Always returns `meta.as_of`. |

**Stores:** PostgreSQL via SQLAlchemy 2.0 async (`asyncpg`), one database per service. Redis for
cache, rate limits, locks and idempotency — nothing durable. Object storage (MinIO) for assets.

**Roles:** `—` public · `S` student · `I` instructor · `A` admin · `owner` = the instructor who
owns the course (ownership check inside the owning service).

★ = `Idempotency-Key` header required.

---

## 2. Service map

| Service | Database | Owns |
| --- | --- | --- |
| `identity` | `identity_db` | users, roles, refresh tokens, JWKS |
| `course` | `course_db` | courses, modules, lessons (metadata **and** body), prerequisites, publish runs |
| `content` | `content_db` | chunks, content search index, uploaded assets |
| `learning` | `learning_db` | enrollments, progress, certificates, `course_cache` |
| `notification` | `notification_db` | notification records, preferences |
| `analytics` | `analytics_db` | projections, failure records |

**Lesson bodies live in `course`, with lesson metadata, in one `jsonb` column.** Splitting body
from metadata across services would mean a dual write with no transaction across it on the most
basic create path in the system. `content` owns only derived artifacts, all rebuildable.

---

## 3. Identity

| Endpoint | Purpose | Roles | Type | Owner |
| --- | --- | --- | --- | --- |
| `POST /auth/register` | Create account | — | Simple + Event | identity |
| `POST /auth/login` | Issue token pair | — | Simple | identity |
| `POST /auth/refresh` | Rotate token pair | — | Simple | identity |
| `POST /auth/logout` | Revoke token family | S I A | Simple | identity |
| `GET /auth/me` | Current user + permissions | S I A | Simple | identity |
| `POST /auth/password/forgot` | Request reset | — | Simple + Event | identity |
| `POST /auth/password/reset` | Set new password | — | Simple | identity |
| `GET /.well-known/jwks.json` | Public signing keys | — | Simple | identity |
| `GET /users` | List users | A | Simple | identity |
| `GET /users/{id}` | Detail | A, self | Simple | identity |
| `PATCH /users/{id}` | Update profile | A, self | Simple | identity |
| `PATCH /users/{id}/role` | Change role | A | Simple + Event | identity |
| `POST /instructor-requests` | Request instructor access | S | Simple + Event | identity |
| `GET /instructor-requests` | List pending | A | Simple | identity |
| `POST /instructor-requests/{id}/approve` | Grant instructor role | A | Simple + Event | identity |

The last three close a gap in the original design: the brief requires three roles but never said
how someone *becomes* an instructor. Self-request with admin approval is the assumed model —
confirm with the client, since admin-created or invite-code are equally valid.

`/auth/me` is the only place a client learns its permission set. Clients never infer permissions
from the role string.

---

## 4. Courses, modules, lessons

| Endpoint | Purpose | Roles | Type | Owner |
| --- | --- | --- | --- | --- |
| `POST /courses` | Create draft | I | Simple | course |
| `GET /courses` | Catalogue (`?q=&level=&status=`) | — | Simple | course |
| `GET /courses/{id}` | Detail + module tree | — | Simple | course |
| `PATCH /courses/{id}` | Update metadata | owner, A | Simple + Event | course |
| `DELETE /courses/{id}` | Archive (soft) | owner, A | Simple + Event | course |
| `GET /courses/{id}/prerequisites` | List | — | Simple | course |
| `PUT /courses/{id}/prerequisites` | Replace set | owner, A | Simple + Event | course |
| `POST /courses/{id}/modules` | Add module | owner | Simple | course |
| `GET /courses/{id}/modules` | List | — | Simple | course |
| `PATCH /modules/{id}` | Update | owner | Simple | course |
| `DELETE /modules/{id}` | Remove | owner | Simple | course |
| `PUT /courses/{id}/modules/order` | Reorder | owner | Simple | course |
| `POST /modules/{id}/lessons` | Add lesson (metadata + body) | owner | Simple | course |
| `GET /lessons/{id}` | Lesson detail | enrolled S, owner, A | Simple | course |
| `PATCH /lessons/{id}` | Update | owner | Simple | course |
| `DELETE /lessons/{id}` | Remove | owner | Simple | course |

Catalogue search is Postgres `tsvector` over title, description and tags. Lesson body is a
`jsonb` column with a `lesson_type` discriminator and a `CHECK` constraint per supported type —
`jsonb` without a constraint is a dumping ground.

`PATCH /courses/{id}` and prerequisite changes emit events because `learning` mirrors capacity
and prerequisites in its `course_cache`.

### Content versioning — the gap this closes

The brief says content is processed when an instructor "publishes **or updates** a course". In
the original design, editing a published course left stored chunks and the search index silently
diverged from the live content.

**Resolution: editing a published course creates a new draft version.** The published version
stays live and unchanged; the draft must be published again to take effect. `course_version` is
recorded on every lesson. This is how a real LMS behaves — students never see a half-edited
course mid-lesson — and it also answers the open question about whether enrolled students see
edits immediately (they see the published version they are on until the instructor republishes).

Endpoints this adds:

| Endpoint | Purpose | Roles | Type | Owner |
| --- | --- | --- | --- | --- |
| `POST /courses/{id}/versions` | Start a draft from the live version | owner | Simple | course |
| `GET /courses/{id}/versions` | Version history | owner, A | Simple | course |
| `DELETE /courses/{id}/versions/{v}` | Discard an unpublished draft | owner | Simple | course |

---

## 5. Assets and content processing

| Endpoint | Purpose | Roles | Type | Owner |
| --- | --- | --- | --- | --- |
| `POST /assets/upload-url` | Presigned upload URL | owner | Simple | content |
| `POST /assets` ★ | Confirm upload, register asset | owner | Simple + Event | content |
| `GET /assets/{id}` | Asset metadata + signed URL | enrolled S, owner | Simple | content |
| `DELETE /assets/{id}` | Remove asset | owner | Simple + Event | content |
| `GET /courses/{id}/content/search?q=` | Search inside course content | enrolled S, owner | Simple | content |

This closes two gaps. "Learning assets" is an explicit functional requirement and had no
endpoints; and the brief asks for content "stored in a structure that supports fast retrieval and
search" with no search endpoint anywhere.

Content search is Postgres `tsvector` + GIN over chunks, scoped by `course_id`. Semantic search is
Part B and lives in the future AI service.

**Object storage is a stack addition.** The brief requires uploads but names no blob store.
MinIO (S3-compatible) in compose, metadata in `content_db`. These endpoints are blocked until
that is agreed.

---

## 6. Publishing — the saga

| Endpoint | Purpose | Roles | Type | Owner |
| --- | --- | --- | --- | --- |
| `POST /courses/{id}/publish` ★ | Publish a version | owner, A | **Event** | course |
| `GET /courses/{id}/publish-runs` | Run history | owner, A | Simple | course |
| `GET /courses/{id}/publish-runs/{run_id}` | Per-step status | owner, A | Simple | course |
| `POST /courses/{id}/publish-runs/{run_id}/retry` ★ | Retry a failed run | A | **Event** | course |
| `POST /courses/{id}/publish-runs/{run_id}/cancel` | Cancel in flight | owner, A | **Event** | course |
| `POST /courses/{id}/unpublish` | Withdraw from catalogue | owner, A | Simple + Event | course |

Temporal workflow owned by `course`; extract, chunk and index activities run on `content-tq` in
the `content` service.

```
validate (course) → extract → chunk → persist chunks (content)
  → build search index (content) → mark ready + outbox row (course, one transaction)
```

`status`: `draft → publishing → ready`, or `failed` with the failing step recorded. A version
appears in the catalogue only at `ready`. Failure compensates: remove partial chunks, roll the
status back, keep the previously published version live.

The chunks written here are the seam for the AI service, which will consume `course.published.v1`
and embed them.

---

## 7. Enrollment

| Endpoint | Purpose | Roles | Type | Owner |
| --- | --- | --- | --- | --- |
| `POST /courses/{id}/enrollments` ★ | Enrol | S | Simple + Event | learning |
| `GET /enrollments/me` | My enrollments | S | Simple | learning |
| `GET /enrollments/{id}` | Detail | S (own), owner, A | Simple | learning |
| `DELETE /enrollments/{id}` | Withdraw | S (own), A | Simple + Event | learning |
| `GET /courses/{id}/enrollments` | Roster | owner, A | Simple | learning |
| `GET /users/{id}/enrollment-history` | History incl. withdrawals | S (self), A | Simple | learning |
| `POST /courses/{id}/enrollments/bulk` ★ | Corporate batch | A | **Event** | learning |
| `GET /enrollment-jobs/{job_id}` | Bulk job status | A | Simple | learning |

One transaction in `learning_db`: prerequisite check against local completions, `SELECT … FOR
UPDATE` on the capacity row, unique-constraint insert, progress initialisation, outbox row. No
network call to another service on this path.

| Brief rule | Enforcement |
| --- | --- |
| No duplicate enrollment | Unique constraint on `(student_id, course_id) WHERE NOT withdrawn` |
| Seat limit | Row lock on the `course_cache` capacity row in the same transaction |
| Prerequisites | Query against this service's own completion records |

Capacity comes from `course_cache`, fed by `course.*` events, so it can be seconds stale during a
burst. Rosters use a name snapshot stored at enrollment time rather than calling `identity`.

Bulk enrollment is a Temporal workflow: hundreds of rows, partial failure, resumable status.

---

## 8. Progress and certificates

| Endpoint | Purpose | Roles | Type | Owner |
| --- | --- | --- | --- | --- |
| `GET /enrollments/{id}/progress` | Progress detail | S (own), owner, A | Simple | learning |
| `PUT /enrollments/{id}/lessons/{lid}/progress` | Upsert position | S (own) | Simple | learning |
| `POST /enrollments/{id}/lessons/{lid}/complete` ★ | Mark complete | S (own) | Simple + Event | learning |
| `GET /courses/{id}/progress-summary` | Cohort progress | owner, A | **Projection** | analytics |
| `GET /enrollments/{id}/certificate` | Fetch certificate | S (own), A | Simple | learning |
| `GET /certificates/{code}` | Public verification | — | Simple | learning |

Progress heartbeats are the highest-frequency write in the platform — an idempotent upsert that
emits **no event**. Only completion emits. Emitting per heartbeat would flood Kafka with data
nobody consumes.

Certificates: the record row is written when the last lesson completes; PDF rendering is a Celery
task consuming `course.completed.v1`. Rendering must never run inside the completion request.

---

## 9. Notifications

| Endpoint | Purpose | Roles | Type | Owner |
| --- | --- | --- | --- | --- |
| `GET /notifications/me` | Inbox (`?unread=true`) | S I A | Simple | notification |
| `POST /notifications/{id}/read` | Mark read | record owner | Simple | notification |
| `POST /notifications/read-all` | Mark all read | record owner | Simple | notification |
| `GET /notifications/preferences` | Channel preferences | S I A | Simple | notification |
| `PUT /notifications/preferences` | Update | S I A | Simple | notification |

**No send endpoint, ever.** Records are written only by event consumers. An API that lets a client
send a notification is an abuse vector.

---

## 10. Analytics

Every endpoint is a **Projection** read from `analytics_db`, built by consumers, cached in Redis,
and carrying `meta.as_of`. All nine brief metrics are covered.

| Endpoint | Metric from the brief | Roles | Cache |
| --- | --- | --- | --- |
| `GET /analytics/overview` | Total students, instructors, courses published | A, I | 60s |
| `GET /analytics/enrollments?from=&to=&granularity=` | New enrollments over time | A, owner | 5m |
| `GET /analytics/completion?course_id=` | Course completion rate | A, owner | 5m |
| `GET /analytics/time-to-complete?course_id=` | Average time to complete | A, owner | 5m |
| `GET /analytics/courses/popular?period=` | Most popular courses | A I S | 5m |
| `GET /analytics/students/course-load` | Average courses per student | A | 15m |
| `GET /analytics/courses/{id}` | Per-course instructor dashboard | owner, A | 5m |
| `GET /analytics/failures?type=&from=&to=` | Failed events / workflow issues | A | 60s |
| `POST /analytics/projections/{name}/rebuild` | Replay from the log | A | — |

**Be explicit with the client:** these numbers are eventually consistent, typically seconds
behind. `meta.as_of` makes the lag visible rather than implied. The complaint in the brief is that
dashboards *disagree with reality* — the fix is a bounded, visible, rebuildable lag, not a
pretence of real-time.

`GET /analytics/failures` reads DLQ records and `workflow.failed.v1` events. It is the one metric
that needs the event spine before it can return anything real. Do not stub it with fake data.

`POST .../rebuild` is the recovery story for a projection bug, and the reason Kafka retention
matters.

---

## 11. Operations

| Endpoint | Purpose | Roles |
| --- | --- | --- |
| `GET /health` (per service) | Overall + per-dependency status | — |
| `GET /version` (per service) | Git SHA, build time | — |
| `GET /metrics` (per service) | Prometheus scrape | internal |
| `GET /admin/dlq?service=&topic=` | Inspect dead letters | A |
| `POST /admin/dlq/{id}/replay` ★ | Replay one message | A |
| `POST /admin/dlq/replay` ★ | Bulk replay by filter | A |
| `GET /admin/outbox/stats` | Backlog size, oldest unpublished age, per service | A |
| `GET /admin/workflows?status=failed` | Failed workflow runs | A |

Health and version sit outside `/api/v1` — they are infrastructure contracts and must not move
when the API version does.

`GET /admin/workflows` closes the last requirement gap: the brief asks for the ability to
diagnose failures in background tasks, which needs an endpoint, not just a Jaeger link.

---

## 12. Event catalogue

Avro in Schema Registry, `BACKWARD` compatibility enforced in CI. Topic naming
`smartcourse.<domain>.<event>.v<major>`, partition key = aggregate id. Every event is written to
the producing service's outbox in the same transaction as the state change.

| Event | Producer | Consumers |
| --- | --- | --- |
| `user.registered.v1` | identity | notification, analytics, learning (name snapshot) |
| `user.role_changed.v1` | identity | analytics |
| `instructor.approved.v1` | identity | notification, analytics |
| `course.created.v1` / `course.updated.v1` | course | learning (cache), analytics |
| `course.published.v1` | course | learning (cache), notification, analytics, *(ai)* |
| `course.unpublished.v1` / `course.archived.v1` | course | learning (cache), analytics |
| `content.indexed.v1` | content | course (workflow), *(ai)* |
| `asset.registered.v1` | content | analytics |
| `enrollment.created.v1` | learning | notification, analytics |
| `enrollment.withdrawn.v1` | learning | analytics |
| `progress.updated.v1` | learning | analytics |
| `course.completed.v1` | learning | notification (certificate), analytics |
| `workflow.failed.v1` | any | analytics, alerting |

---

## 13. Idempotency, limits and caching

**`Idempotency-Key` required** on every ★ endpoint: enrol, bulk enrol, lesson completion, publish,
retry, asset confirm, DLQ replay. Key + body hash → stored response in Redis, 24h TTL. Same key
with a different body returns `409`. This is a stated brief requirement and was missing from the
previous revision.

**Rate limits** (Redis sliding window via Lua, per user, IP fallback when anonymous). The brief
names backpressure as a functional requirement, so these are scheduled against the enrollment
milestone, not left to judgement:

| Group | Limit |
| --- | --- |
| Login, register | 5/min per IP + lockout on repeated failure |
| Progress updates | 120/min per user |
| Reads and catalogue | 300/min per user |
| Publish, bulk enrol | 10/hour per user |
| Analytics | 60/min per user |

**Cache TTLs:** catalogue 60s · course detail 300s, invalidated on publish · analytics 60s–15m ·
`/auth/me` 60s.

---

## 14. Open questions for the client

1. **Object storage** — MinIO recommended; not in the stated stack. §5 is blocked on it.
2. **Enrollment approval** — self-service assumed. Corporate approval would add a `pending` state;
   cheap now, expensive once the state machine ships.
3. **Waitlists** — `course_cache` staleness means a burst can exceed capacity by a small margin.
   A waitlist makes that a feature rather than a defect. Decide before launch.
4. **Instructor provisioning** — §3 assumes self-request with admin approval. Confirm.
5. **Certificate verification** — assumed public via code. Confirm.