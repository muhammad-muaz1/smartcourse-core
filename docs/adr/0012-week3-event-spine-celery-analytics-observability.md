# 0012 — Week 3: Kafka event spine, Celery, analytics, observability

- Status: Accepted
- Date: 2026-11-13
- Supersedes: ADR 0011's "course → learning through Temporal" transport; ADR 0001 §6's
  client choice (confluent-kafka)

## Scope

Week-3 deliverables: Kafka integration for events, background workers (Celery), basic
analytics metrics, observability (logs, tracing, metrics). Plus the gaps in the brief's
enrollment workflow that week 2 left open: progress initialisation, notifications,
analytics updates.

Built:

- **libs/contracts**: every cross-service event (`user.registered`, `course.*`,
  `enrollment.created|withdrawn|completed`, `workflow.failed`, the dead-letter envelope),
  a topic → schema registry, committed JSON Schemas and a BACKWARD-compatibility test.
- **libs/platform-core**: `outbox` (mixin, writer, relay), `inbox`, `kafka` (publisher,
  topic provisioning, consumer runner with inbox dedupe, retries, dead-lettering,
  after-commit hooks), `telemetry` (OTel → OTLP/HTTP), `metrics` (Prometheus), a worker
  bootstrap, and `testing` (throwaway Postgres/Redis for machines without Docker).
- **identity**: outbox + worker; `user.registered.v1` written by `create_user`.
- **course**: `course.*` and `workflow.failed.v1` go to Kafka; `lesson_ids` in the
  snapshot; `DeliverCourseEventWorkflow` removed.
- **learning**: Kafka consumer replaces the Temporal activity; progress (lesson
  completions, `completed` state), `enrollment.*` via outbox, `student_directory` for
  roster names, two new endpoints.
- **notification** (new, :8004): recipients and notification records from events,
  delivered by Celery; the inbox API.
- **analytics** (new, :8005): fact tables from every topic; the brief's nine metrics.
- **gateway**: the two new upstreams; upstream calls traced.
- **deploy**: Kafka, RabbitMQ, Jaeger, Prometheus, Grafana in compose; `make kafka`,
  `make jaeger`, `make prometheus` for machines without Docker.

## Decisions

**aiokafka, not confluent-kafka.** ADR 0001 §6 chose confluent-kafka and confined its
blocking calls to dedicated processes. Every process that touches Kafka here — the
relays and the consumers — is asyncio with async SQLAlchemy; with confluent-kafka each
produce/poll would hop to a thread. aiokafka is asyncio-native, so the relay and the
consumer are one event loop with the database work they coordinate. If Avro arrives,
confluent's Schema Registry client is plain HTTP and works alongside aiokafka.

**No Schema Registry server yet; the contract check runs in CI instead.** Contracts stay
Pydantic, serialised as JSON. Each one's JSON Schema is committed under
`libs/contracts/schemas/` and `tests/test_schemas.py` fails any change that isn't
BACKWARD compatible (removed field, changed type, newly required field) — the check a
registry makes, without another JVM process to run. A registry with JSON Schema (or
Avro) is a transport change in `platform_core.kafka` plus a CI step; the committed
schemas are what it would be seeded with.

**One consumer group per service, one handler per topic.** The inbox claim is keyed by
(event id, group), so two modules of one service handling the same topic would share a
claim and the second would silently never run. `merge_handlers` refuses that at start-up.

**Retry in place, then park.** A failing message is retried in-process with capped
exponential backoff (default 5 attempts) while its partition waits — order within an
aggregate matters more than throughput. Then it goes to `smartcourse.deadletter.v1`
with the error, and the offset moves on. Unparseable messages skip the retries.
Messages *on* the dead-letter topic are never dead-lettered again (that loops while the
database is down); they retry until they land. Analytics consumes every dead letter, so
`/analytics/failures` is the "failed events" metric.

**Topic retention: forever for events, 30 days for dead letters.** A new consumer group
starts from the earliest offset, so a new or rebuilt projection fills from history.
Expired history can't be replayed. Workers create missing topics on start
(`auto.create.topics.enable=false` on the broker, so a typo can't create one).

**`enrollment.completed.v1`, not `course.completed.v1`.** The design's catalogue names
the completion fact `course.completed.v1`, but `smartcourse.course.*` topics belong to
the course service. The fact is about learning's aggregate, so it carries its name.

**Progress lives in learning, measured against the lesson list in `course_cache`.**
Course's snapshot now carries `lesson_ids` (an optional field: v1-compatible). Enrolling
initialises `lessons_completed = 0` in the enrollment transaction. Completing a lesson is
an idempotent insert plus a recount under the enrollment's row lock; the last one moves
the enrollment to `completed` and writes `enrollment.completed.v1`, in one transaction.
A completed enrollment still blocks a duplicate (the unique index is now
`WHERE status <> 'withdrawn'`) and can't be withdrawn.

**Analytics stores facts, not counters.** One row per user, course and enrollment, keyed
by the entity's id, upserted only if the event's `occurred_at` is not older than the row's
`as_of`. A counter double-counts on any replay that gets past the inbox and can never be
repaired; a fact table is idempotent, and every metric is a query over it. Completion
rate is `completed / (enrollments − withdrawn)` — a stated definition, shown with its
inputs. `meta.as_of` on every metric is when analytics last applied an event.

**Celery runs async service code on one event loop per worker process.** The database
engine is created lazily in each forked child (asyncpg connections can't cross a fork).
Tasks are late-acked, re-queued if their worker dies, time-limited, retried with
full-jitter backoff, and on the last failure record it on the row and write
`workflow.failed.v1`. A consumer enqueues only **after its transaction commits**
(`platform_core.kafka.after_commit`), so a task never races ahead of its row. If that
enqueue fails, a beat sweep re-enqueues rows still pending after 10 minutes.

**Celery's broker: RabbitMQ in compose, Redis allowed locally.** The brief names
RabbitMQ and compose runs it. A machine without Docker or RabbitMQ can point
`SMARTCOURSE_CELERY__BROKER_URL` at Redis (env.example does). Redis "holds nothing
durable" (CLAUDE.md §6) — acceptable for local work only, because a Redis flush drops
queued deliveries.

**Delivery is "log" only.** No SMTP relay exists to send through; the log backend
records the delivery (notification id, never the address or body). The record, retry
and failure machinery is real; an SMTP backend is one class behind the `Mailer` protocol.

**Old facts are recorded, not sent.** A welcome for something more than 24 hours old is
stored as `skipped`. Without this a replay or `make backfill` would greet every existing
user again.

**Password-reset tokens stay out of Kafka.** Identity keeps its log-only sender for reset
links: a reset token on a topic retained forever would be a credential in the event log.
Only the welcome moved to notification.

**Tracing: one trace from the edge to the last worker.** Trace context is captured into
the outbox row's `headers` in the request's transaction; the relay continues that trace
in a producer span and injects it into the Kafka headers; consumers continue it; Celery
continues it through task headers; Temporal through its interceptor. Verified: one
`POST /auth/register` is a single trace across gateway, identity, identity-worker, the
three consumers and the Celery task. Every log line inside a span carries `trace_id` and
`span_id`.

**Metrics on prometheus-client** (ADR 0001 §7). APIs serve `/metrics`; workers serve it
on their own port (9101–9105); Celery's prefork children use multiprocess mode, served by
the parent on 9114. HTTP labels use the route template, never the raw path.

**Backfill for data that predates the spine.** `make backfill` re-announces every
existing user, course and enrollment through the normal outboxes. Consumers upsert, so a
repeat is harmless to projections.

## Not done in this change

- **Schema Registry server**, Avro — see above.
- **`POST /analytics/projections/{name}/rebuild`** and the `/admin/dlq` replay endpoints.
  Rebuild today: drop the projection rows, reset the `analytics` group's offsets to
  earliest (`kafka-consumer-groups.sh --reset-offsets --to-earliest`), restart the worker.
- **Notification preferences**, SMTP delivery, certificates (PDF rendering via Celery).
- **Prerequisites**, bulk enrollment, content service publish steps, rate limiting —
  as listed in ADR 0011.
- **Lesson access for "enrolled students only"** in course (needs `enrollment.*` feeding
  course, or the check moving to learning).
- **Alerting rules** in Prometheus; the dashboard is provisioned, alerts are not.

## Consequences

- Local dev needs Kafka (`make kafka`), and Jaeger for traces (`make jaeger`). `make dev`
  now runs six APIs, five workers and the Celery worker.
- `make env` appends new keys to existing `.env` files; it never changes a value.
- An event consumer is a module's `consumers.py` exposing `CONSUMERS`; a Celery task is a
  module's `tasks.py` — both auto-discovered like routes.
- `make test-local` runs every suite without Docker against a throwaway Postgres cluster
  and Redis in `.tools/`.
