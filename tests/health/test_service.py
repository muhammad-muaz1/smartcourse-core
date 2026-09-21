import asyncio

import pytest

from src.config import HealthSettings, Settings
from src.modules.health import service


@pytest.fixture(autouse=True)
def _isolated_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "_cache", {})


def _patch_health_settings(monkeypatch: pytest.MonkeyPatch, *, timeout: float, ttl: float) -> None:
    settings = Settings(
        _env_file=None, health=HealthSettings(check_timeout_seconds=timeout, cache_ttl_seconds=ttl)
    )
    monkeypatch.setattr(service, "get_settings", lambda: settings)


async def _ok() -> None:
    return None


async def _boom() -> None:
    raise RuntimeError("dependency exploded")


async def _slow() -> None:
    await asyncio.sleep(10)


async def test_check_health_is_healthy_when_all_checks_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_health_settings(monkeypatch, timeout=1.0, ttl=5.0)
    monkeypatch.setattr(service, "_CHECKS", {"fake": _ok})

    healthy, services = await service.check_health()

    assert healthy is True
    assert services["fake"].status == "ok"
    assert services["fake"].latency_ms is not None


async def test_check_health_is_unhealthy_when_one_check_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_health_settings(monkeypatch, timeout=1.0, ttl=5.0)
    monkeypatch.setattr(service, "_CHECKS", {"good": _ok, "bad": _boom})

    healthy, services = await service.check_health()

    assert healthy is False
    assert services["good"].status == "ok"
    assert services["bad"].status == "error"
    assert services["bad"].detail == "dependency exploded"


async def test_check_health_times_out_instead_of_hanging(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_health_settings(monkeypatch, timeout=0.05, ttl=5.0)
    monkeypatch.setattr(service, "_CHECKS", {"slow": _slow})

    healthy, services = await service.check_health()

    assert healthy is False
    assert services["slow"].status == "error"
    assert services["slow"].detail == "timed out"


async def test_check_health_caches_result_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    async def counted() -> None:
        calls["n"] += 1

    _patch_health_settings(monkeypatch, timeout=1.0, ttl=10.0)
    monkeypatch.setattr(service, "_CHECKS", {"counted": counted})

    await service.check_health()
    await service.check_health()

    assert calls["n"] == 1


async def test_check_health_calls_again_after_ttl_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    async def counted() -> None:
        calls["n"] += 1

    _patch_health_settings(monkeypatch, timeout=1.0, ttl=0.0)
    monkeypatch.setattr(service, "_CHECKS", {"counted": counted})

    await service.check_health()
    await service.check_health()

    assert calls["n"] == 2
