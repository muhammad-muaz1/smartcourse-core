# 0001 — Architecture decisions

- Status: **Partially superseded by [0006](0006-scaffold-simplification.md)** (2026-09-18)
  — the folder structure, middleware list, and Redis rate-limiting decisions below
  describe the pre-simplification scaffold. The data-placement rules (decision 2) and
  the three-async-mechanism rule (decision 3) still hold; the file layout does not.
- Date: 2026-09-17

## Context

SmartCourse Core is a modular monolith for a learning platform that must survive
course-launch traffic spikes, keep course/enrollment/analytics data consistent across
Postgres, MongoDB and Redis, and make background work (publishing, enrollment side
effects, notifications) traceable and recoverable. `claude/CLAUDE.md` §1–§9 already
specifies most of the architecture in binding form; this ADR records the decisions as a
single reference and captures the reasoning that isn't self-evident from the rules
themselves, plus the points verified against current library behaviour before Phase 1
starts.

## Decisions

**1. Modular monolith, one image, six process types.**
`api`, `celery-worker`, `celery-beat`, `temporal-worker`, `kafka-consumer`,
`outbox-relay` are entrypoints into the same image (CLAUDE.md §1, §12). Modules under
`modules/<name>/` never import each other's internals; they communicate through events
or a published service interface. `core/` and `db/` never import from `modules/`.
Dependencies point inward: `api → service → repository → model`.

**2. Data placement by invariant strength, not convenience.**
Postgres holds anything with a commit-time invariant (users, enrollments, outbox,
inbox, idempotency keys). MongoDB holds open-shaped/derived data that is rebuildable
from Postgres plus the event log (content chunks, projections, analytics rollups,
notification payloads). Redis holds nothing durable. No foreign keys cross stores;
Mongo documents reference Postgres rows by UUID. See ADR 0002 for the Mongo driver
choice this implies.

**3. Three async mechanisms, one job each, never substituted.**
Temporal orchestrates (multi-step, must survive a restart mid-flight). Kafka informs
(broadcast a fact to unknown consumers). Celery executes (one retryable unit, seconds
not hours). Course publishing is a Temporal workflow; notifications triggered by a
publish are Celery tasks fired from a Kafka consumer, so a mail outage can never fail a
publish.

**4. The API never touches Kafka directly.**
State changes that must emit an event write the domain row and an `outbox_messages` row
in the same transaction. Only the `outbox-relay` process produces to Kafka, polling with
`SELECT ... FOR UPDATE SKIP LOCKED` against a partial index on
`(published_at) WHERE published_at IS NULL`. This is what makes cross-store consistency
real instead of best-effort.

**5. Three idempotency mechanisms, kept distinct.**
Consumer inbox (`processed_events(event_id, consumer_group)`) stops double-processing.
`Idempotency-Key` + body-hash in Redis (24h TTL) makes unsafe HTTP verbs replay-safe.
Natural-key DB constraints make the duplicate impossible regardless of what application
code does. These solve different failure modes (broker redelivery, client retry,
programming error) and collapsing them into one mechanism would silently drop one of
those guarantees.

**6. confluent-kafka is not asyncio-native — this changes where it may be called from.**
Verified against confluent-kafka 2.15.1: `Producer.produce()` / `Consumer.poll()` are
still blocking calls into the librdkafka C extension; "asynchronous" in the `produce()`
docstring refers to the delivery-callback model, not Python `asyncio`. There is no
asyncio-native `Producer`/`Consumer`. (The library does ship `AsyncSchemaRegistryClient`
/ `AsyncAvroSerializer` for the Schema Registry's own HTTP calls, which use `httpx` and
are legitimately awaitable — those are unrelated to the produce/poll path.)
Consequence: the blocking client only ever runs inside the dedicated `kafka-consumer`
and `outbox-relay` processes, which own a plain blocking loop. It must never be called
from the `api` process's event loop. If a future need arises to call it from async code
anyway, wrap the call in `asyncio.to_thread`, but the base design avoids that need by
construction (decision 4).

**7. Metrics via `prometheus-client`, not the OTel Prometheus bridge.**
`opentelemetry-exporter-prometheus` is still `0.65b0` (pre-1.0, matching the rest of the
Python OTel instrumentation packages, which are all beta by convention). Tracing goes
through OTel → OTLP → Jaeger as CLAUDE.md §9 specifies, but the Prometheus metrics
registry is built directly on `prometheus-client` (stable, 0.26.0) and scraped at
`/metrics`. This keeps the one metrics path that ops depends on for alerting on a
stable library, and keeps OTel scoped to what it's actually needed for here: traces.

**8. API conventions, security and observability rules are taken as specified.**
RFC 9457 problem details, the `{"data", "meta"}` envelope, cursor pagination, RS256 JWT
with `kid` + JWKS, Argon2id, and refresh rotation with reuse detection revoking the
whole token family (CLAUDE.md §7–§9) are adopted without modification — nothing found
during verification conflicts with them.

**9. Dependency pinning strategy.**
`pyproject.toml` constrains each dependency to its current major version (e.g.
`fastapi>=0.141,<0.142` where a library hasn't reached 1.0 and moves fast; `>=2,<3` for
libraries with a stable major line such as SQLAlchemy, Pydantic, Celery). `uv.lock`
pins exact resolved versions. `uv lock --upgrade` stays inside tested compatibility
until a deliberate major-version bump.

## Open disagreements / risks flagged back to the requester

These are called out per the Phase 0 instruction to disagree now rather than later.
None of them block Phase 1; they're flagged for a decision or acknowledgement.

- **CLAUDE.md's location.** The build brief said CLAUDE.md is "committed at the repo
  root," but it is actually at `claude/CLAUDE.md`. I have not moved it — that's a
  one-line call for you, not an architecture decision, but tools that auto-load
  `CLAUDE.md` from repo root (including this one, ordinarily) won't pick it up from
  `claude/` implicitly.
- **mypy jumped 1.9 → 2.0 this year.** Current is `2.3.1`. Nothing in the 2.x
  changelog looked incompatible with `--strict` on a fresh codebase, but it's a recent
  major bump worth knowing about if a teammate's toolchain is pinned to 1.x.
- **OTel instrumentation packages are all pre-1.0** (`0.65b0`). This is normal for this
  ecosystem, not a red flag specific to this project, but it means every instrumentor
  import (`opentelemetry-instrumentation-fastapi`, `-sqlalchemy`, `-celery`) is pinned
  exactly rather than range-pinned, since beta packages don't hold a stable API across
  minor versions.
- **No functional disagreement with CLAUDE.md's rules themselves** — §2–§9 are
  internally consistent and match current library capabilities as verified. The
  scaffold-only ambiguity that remains is *how much* of `core/telemetry` to wire in
  Phase 1 vs Phase 2; that's addressed in the Phase 1 plan, not here.
