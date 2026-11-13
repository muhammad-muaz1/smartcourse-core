"""The Celery process plumbing: one event loop per worker process, re-created after a
fork, and the queue/ack configuration the brief's reliability rests on. No broker."""

import os

import pytest

from src import celery_app


def test_tasks_ack_late_and_survive_a_dead_worker() -> None:
    conf = celery_app.celery_app.conf
    assert conf.task_acks_late is True
    assert conf.task_reject_on_worker_lost is True
    assert conf.worker_prefetch_multiplier == 1
    assert {queue.name for queue in conf.task_queues} == {"notifications", "maintenance"}


def test_the_sweep_is_scheduled_on_the_maintenance_queue() -> None:
    [entry] = celery_app.celery_app.conf.beat_schedule.values()
    assert entry["task"] == "notification.requeue_stale.v1"
    assert entry["options"]["queue"] == "maintenance"


def test_async_code_runs_on_one_loop_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    inits: list[object] = []
    monkeypatch.setattr(celery_app.postgres, "init_postgres", inits.append)
    monkeypatch.setattr(celery_app, "_loop", None)

    async def loop_id() -> int:
        import asyncio

        return id(asyncio.get_running_loop())

    first, second = celery_app.run_async(loop_id()), celery_app.run_async(loop_id())
    assert first == second and len(inits) == 1

    # A forked child sees a different pid: it must build its own loop and engine.
    monkeypatch.setattr(celery_app, "_loop_pid", os.getpid() + 1)
    celery_app.run_async(loop_id())
    assert len(inits) == 2


def test_process_hooks_configure_logging_and_tracing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)
    celery_app._on_worker_init()
    celery_app._on_child_init()
    celery_app._on_child_shutdown(pid=12345)  # no multiprocess dir: nothing to mark
