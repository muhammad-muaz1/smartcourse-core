"""The consumer → Celery seam and the task's retry policy. The broker and the task's
event loop are replaced; the decisions under test are ours: enqueue only after commit,
retry with backoff, give up and record after max_retries."""

import uuid
from collections.abc import Coroutine
from typing import Any

import pytest
from celery.exceptions import Retry
from sqlalchemy import select

from platform_core.kafka import EventMessage
from src.db import postgres
from src.modules.notifications import consumers, tasks
from src.modules.notifications.models import Notification
from tests.notifications.helpers import user_event


def _message(value: bytes) -> EventMessage:
    return EventMessage(
        topic="smartcourse.user.registered.v1",
        partition=0,
        offset=0,
        key=None,
        value=value,
        headers={},
    )


async def test_the_consumer_enqueues_only_after_its_transaction_commits(
    _db: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    enqueued: list[uuid.UUID] = []

    async def fake_enqueue(notification_id: uuid.UUID) -> None:
        enqueued.append(notification_id)

    monkeypatch.setattr(consumers, "enqueue_delivery", fake_enqueue)
    event = user_event()
    async with postgres.database.session_scope() as session:
        await consumers.CONSUMERS["smartcourse.user.registered.v1"](
            session, _message(event.model_dump_json().encode())
        )
        callbacks = session.info.pop("smartcourse.after_commit")
        assert enqueued == []  # nothing escapes before commit
    for callback in callbacks:
        await callback()

    async with postgres.database.session_scope() as session:
        [row] = (await session.execute(select(Notification))).scalars()
    assert enqueued == [row.id]


class _FakeTask:
    """Stands in for Celery's bound ``self``."""

    def __init__(self, retries: int) -> None:
        self.request = type("Request", (), {"retries": retries})()

    def retry(self, *, exc: Exception, countdown: int) -> Retry:
        assert 1 <= countdown <= tasks.settings.celery.retry_backoff_max_seconds
        return Retry(exc=exc, when=countdown)


def _run_async_returning(outcomes: list[Any]) -> Any:
    calls: list[str] = []

    def fake(coro: Coroutine[Any, Any, Any]) -> Any:
        calls.append(coro.cr_code.co_name)
        coro.close()
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    fake.calls = calls  # type: ignore[attr-defined]  # inspection hook for the test
    return fake


def test_a_successful_attempt_reports_the_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks, "run_async", _run_async_returning(["sent"]))
    assert tasks.deliver.run.__func__(_FakeTask(0), str(uuid.uuid4())) == "sent"  # type: ignore[attr-defined]  # Celery binds run at registration


def test_a_failed_attempt_is_recorded_and_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _run_async_returning([ConnectionError("down"), None])
    monkeypatch.setattr(tasks, "run_async", fake)
    with pytest.raises(Retry):
        tasks.deliver.run.__func__(_FakeTask(0), str(uuid.uuid4()))  # type: ignore[attr-defined]  # see above
    assert fake.calls == ["_deliver", "_record_failure"]


def test_the_last_attempt_gives_up_and_records_it(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _run_async_returning([ConnectionError("down"), None, None])
    monkeypatch.setattr(tasks, "run_async", fake)
    final = _FakeTask(tasks.settings.celery.max_retries)
    assert tasks.deliver.run.__func__(final, str(uuid.uuid4())) == "failed"  # type: ignore[attr-defined]  # see above


def test_backoff_is_jittered_and_capped() -> None:
    cap = tasks.settings.celery.retry_backoff_max_seconds
    values = {tasks._backoff(10) for _ in range(50)}
    assert all(1 <= value <= cap for value in values)
    assert len(values) > 1
