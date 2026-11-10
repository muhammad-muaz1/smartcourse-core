import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from platform_core.config import AppSettings, HealthSettings
from platform_core.health import Check, HealthChecker, build_health_router


def _checker(checks: dict[str, Check], *, timeout: float = 1.0, ttl: float = 5.0) -> HealthChecker:
    return HealthChecker(
        checks, HealthSettings(check_timeout_seconds=timeout, cache_ttl_seconds=ttl)
    )


async def _ok() -> None:
    return None


async def _boom() -> None:
    raise RuntimeError("dependency exploded")


async def _slow() -> None:
    await asyncio.sleep(10)


async def test_healthy_when_all_checks_pass() -> None:
    healthy, services = await _checker({"fake": _ok}).run()

    assert healthy is True
    assert services["fake"].status == "ok"
    assert services["fake"].latency_ms is not None


async def test_unhealthy_when_one_check_fails() -> None:
    healthy, services = await _checker({"good": _ok, "bad": _boom}).run()

    assert healthy is False
    assert services["good"].status == "ok"
    assert services["bad"].status == "error"
    assert services["bad"].detail == "dependency exploded"


async def test_times_out_instead_of_hanging() -> None:
    healthy, services = await _checker({"slow": _slow}, timeout=0.05).run()

    assert healthy is False
    assert services["slow"].detail == "timed out"


async def test_caches_result_within_ttl() -> None:
    calls = {"n": 0}

    async def counted() -> None:
        calls["n"] += 1

    checker = _checker({"counted": counted}, ttl=10.0)
    await checker.run()
    await checker.run()

    assert calls["n"] == 1


async def test_calls_again_after_ttl_expires() -> None:
    calls = {"n": 0}

    async def counted() -> None:
        calls["n"] += 1

    checker = _checker({"counted": counted}, ttl=0.0)
    await checker.run()
    await checker.run()

    assert calls["n"] == 2


def _client(checks: dict[str, Check]) -> TestClient:
    app = FastAPI()
    router = build_health_router(
        checks=checks,
        health=HealthSettings(),
        app=AppSettings(service_name="widgets", git_sha="abc123", build_time="now"),
    )
    app.include_router(router)
    return TestClient(app)


def test_health_route_is_200_when_healthy_and_names_the_service() -> None:
    response = _client({"postgres": _ok}).get("/health")

    assert response.status_code == 200
    assert response.json()["service"] == "widgets"
    assert response.json()["version"] == "abc123"


def test_health_route_is_503_when_a_dependency_is_down() -> None:
    response = _client({"postgres": _ok, "redis": _boom}).get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "error"
    assert response.json()["services"]["redis"]["detail"] == "dependency exploded"


def test_version_route_needs_no_dependency() -> None:
    response = _client({"postgres": _boom}).get("/version")

    assert response.status_code == 200
    assert response.json() == {"service": "widgets", "version": "abc123", "build_time": "now"}
