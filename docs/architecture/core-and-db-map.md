# What each file in `core/` and `db/` does

Every file below has exactly one job. That's deliberate — CLAUDE.md §3 specifies this
breakdown file-by-file (e.g. "middleware/ — one file per middleware",
"db/postgres/ — engine, session, Base, mixins, unit_of_work, repository"), so a business
module never has to guess which of several overlapping files owns a piece of logic.
This page exists so that mapping doesn't have to be reconstructed by reading every file.

## `core/` — framework-level, no domain knowledge

| File | One job |
| --- | --- |
| `config/settings.py` | All configuration, nested by concern (`app`, `postgres`, `redis`, ...). |
| `context.py` | Bind/read `request_id`/`user_id`/`tenant_id` via structlog's contextvars — never passed manually. |
| `logging/setup.py` | Wire structlog → stdlib logging so uvicorn's own logs come out as the same JSON. |
| `telemetry/tracing.py` | OTel SDK → OTLP exporter setup, called once at startup. |
| `telemetry/metrics.py` | The Prometheus `CollectorRegistry` and the two HTTP metrics recorded per request. |
| `health.py` | Generic per-check timeout + TTL cache — used by all three stores' health checks, not store-specific itself. |
| `errors/codes.py` | The stable `ErrorCode` enum — the "machine-readable string" every error response carries. |
| `errors/exceptions.py` | The `AppError` hierarchy domain code raises. Never imports FastAPI. |
| `errors/problem_details.py` | Builds the RFC 9457 response body/shape. |
| `errors/handlers.py` | Maps each exception type to an HTTP status and registers the handlers — the *only* place that mapping exists. |
| `responses/envelope.py` | The `{"data", "meta"}` wrapper every success response uses. |
| `responses/meta.py` | What goes in that `meta` block (request id, timestamp). |
| `responses/pagination.py` | Opaque cursor encode/decode + the `CursorPage` shape. |
| `middleware/*.py` | One file per middleware in CLAUDE.md §7's list — see that file's own docstring for what it does and why it's at its specific position in the stack (order is load-bearing, see docs/adr/0003). |
| `utils/clock.py` / `utils/ids.py` | The only two allowed sources of "now" and "new id". Not a junk drawer — nothing else goes here. |

## `db/` — one subfolder per store, same shape in each

| File | One job |
| --- | --- |
| `postgres/base.py` | The `Base` class + naming convention every ORM model inherits. |
| `postgres/mixins.py` | `UUIDPrimaryKeyMixin` / `TimestampMixin` / `SoftDeleteMixin` — composed into models, not inherited alone. |
| `postgres/engine.py` | Builds the async engine from settings. Nothing else. |
| `postgres/session.py` | Builds the session factory from an engine. Nothing else. |
| `postgres/unit_of_work.py` | One transaction per `async with` block — commit clean, rollback on exception. |
| `postgres/repository.py` | Generic get/add/delete/list_all over one model. Model-specific queries go in that module's own `repository.py`, not here. |
| `postgres/health.py` | `SELECT 1` (readiness) and "is the schema at head" (startup). |
| `mongo/client.py` | Builds the `AsyncMongoClient` + database handle from settings. |
| `mongo/document_base.py` | `BaseDocument` — the timestamp fields every Beanie document inherits. |
| `mongo/index_registration.py` | Calls `init_beanie` with document models sorted for deterministic index creation. |
| `mongo/migration_runner.py` | Applies a list of `Migration`s once each, tracked in the `_migrations` collection. |
| `mongo/migration_loader.py` | Turns `migrations/mongo/*.py` files into that list. Kept separate from the runner, which doesn't care where migrations came from. |
| `mongo/health.py` | Ping (readiness) and "are the known migrations applied" (startup). |
| `redis/client.py` | Builds the Redis client from settings. |
| `redis/rate_limiter.py` | The Lua sliding-window script + its Python wrapper. |
| `redis/cache.py` | Generic get/set/delete-with-TTL for business modules (analytics rollups etc.) — not the same cache as the HTTP idempotency response cache, which lives in `core/middleware/idempotency.py`. |
| `redis/locks.py` | Single-instance distributed lock (SET NX PX + compare-and-delete release). |
| `redis/health.py` | `PING`. |

If a file here ever grows past one clear job, that's the signal to split it, not to add
a second responsibility to it.
