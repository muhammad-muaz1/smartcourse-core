# Where shared mechanics live

Each service has a thin `core/` and `db/`; the machinery behind them is in
`libs/platform-core` (`platform_core`), so a fix lands in every service at once. Nothing in
`platform_core` knows about a user, a course or any other domain concept — if it would,
it belongs in a service.

## `platform_core` — mechanics only

| Module | One job |
| --- | --- |
| `config.py` | Settings groups every service composes (`AppSettings`, `PostgresSettings`, `JWTVerifySettings`, ...). A service subclasses one to change a default. |
| `logging.py` | structlog → stdlib wiring, `request_id` in a contextvar, `service.name` on every line. |
| `middleware.py` | `RequestContextMiddleware`, and `install_middleware()` — the one place the LIFO add order (docs/adr/0003) is encoded. |
| `exceptions.py` | The `AppError` hierarchy service code raises. Never imports FastAPI. |
| `handlers.py` | Maps each error code to an HTTP status — the *only* place that mapping exists. |
| `response.py` | The `{"data", "meta"}` envelope and opaque cursor pagination. |
| `routing.py` | Auto-discovers a service's `src/modules/*/routes.py` and mounts each `router` under `/api/v1`. |
| `security.py` | Offline access-token verification, `CurrentUser`, the role-check dependency. Issuing tokens is identity's alone. |
| `db.py` | `PostgresDatabase` (engine, one-transaction-per-request session, `check()`), `RedisConnection`, `UUIDMixin`, `TimestampMixin`, naming convention. |
| `health.py` | `GET /health` (concurrent checks, per-check timeout, short cache) and `GET /version`. |

## Inside each service

| File | One job |
| --- | --- |
| `src/config.py` | This service's settings and defaults (name, port, database). Reads this service's `.env` only. |
| `src/app.py` | `create_app()` — logging, handlers, middleware, `/health`, auto-discovered routes. |
| `src/main.py` | Entrypoint; `python -m src.main` execs into uvicorn (docs/adr/0007). |
| `src/core/router.py` | Points `platform_core.routing` at this service's `src/modules`. |
| `src/core/dependencies.py` | This service's `get_current_user` / `require_roles` (identity adds its denylist check). |
| `src/db/base.py` | This service's own `Base` — its own metadata, for its own database. |
| `src/db/postgres.py`, `src/db/redis.py` | This service's connection instances. |
| `src/modules/<name>/` | `routes.py` → `service.py` → `models.py`, plus `schemas.py`. |
| `migrations/`, `alembic.ini` | This service's Alembic chain, against its own database only. |
