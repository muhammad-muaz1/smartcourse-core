"""Concurrent per-dependency checks with a timeout and a short result cache.

This lives here, not in core/, because health is its only caller today — CLAUDE.md §2
rule 3: shared infrastructure moves to core/ on the *second* use, not the first. If
another module ever needs the same timeout+cache pattern, that's the signal to promote it.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable

from src.config import get_settings
from src.db import mongo, postgres
from src.db import redis as redis_db
from src.modules.health.schemas import ServiceStatus

_CHECKS: dict[str, Callable[[], Awaitable[None]]] = {
    "postgres": postgres.check,
    "mongo": mongo.check,
    "redis": redis_db.check,
}

_cache: dict[str, tuple[ServiceStatus, float]] = {}


async def _check_one(name: str, check_fn: Callable[[], Awaitable[None]]) -> ServiceStatus:
    settings = get_settings().health
    now = time.monotonic()

    cached = _cache.get(name)
    if cached is not None and (now - cached[1]) < settings.cache_ttl_seconds:
        return cached[0]

    start = time.perf_counter()
    try:
        async with asyncio.timeout(settings.check_timeout_seconds):
            await check_fn()
    except TimeoutError:
        result = ServiceStatus(status="error", detail="timed out")
    except Exception as exc:  # noqa: BLE001 - any dependency failure means "error", not a crash
        result = ServiceStatus(status="error", detail=str(exc))
    else:
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        result = ServiceStatus(status="ok", latency_ms=latency_ms)

    _cache[name] = (result, now)
    return result


async def check_health() -> tuple[bool, dict[str, ServiceStatus]]:
    names = list(_CHECKS)
    results = await asyncio.gather(*(_check_one(name, _CHECKS[name]) for name in names))
    services = dict(zip(names, results, strict=True))
    healthy = all(service.status == "ok" for service in services.values())
    return healthy, services
