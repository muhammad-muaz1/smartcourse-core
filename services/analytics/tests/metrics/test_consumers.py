"""Every registered topic has a handler, and each handler parses its contract and projects
it. Requires Docker or SMARTCOURSE_TEST_POSTGRES_URL."""

import uuid

import pytest
from pydantic import BaseModel
from sqlalchemy import func, select

from platform_core.kafka import EVENT_ID_HEADER, EventMessage, NonRetryableError
from smartcourse_contracts.registry import ALL_TOPICS
from smartcourse_contracts.topics import DEAD_LETTER_TOPIC, topic_for
from src.db import postgres
from src.modules.metrics.consumers import CONSUMERS
from src.modules.metrics.models import CourseFact, EnrollmentFact, FailureRecord, UserFact
from tests.metrics.helpers import course, dead_letter, enrollment, user, workflow_failure


def _message(topic: str, event: BaseModel, *, with_id: bool = True) -> EventMessage:
    return EventMessage(
        topic=topic,
        partition=0,
        offset=0,
        key=None,
        value=event.model_dump_json().encode(),
        headers={EVENT_ID_HEADER: str(uuid.uuid4())} if with_id else {},
    )


def test_analytics_projects_every_registered_topic() -> None:
    assert set(CONSUMERS) == set(ALL_TOPICS)


async def test_each_handler_projects_its_event(_db: None) -> None:
    course_id = uuid.uuid4()
    messages = [
        _message(topic_for("user.registered.v1"), user("student")),
        _message(topic_for("course.published.v1"), course(course_id, instructor_id=uuid.uuid4())),
        _message(topic_for("enrollment.created.v1"), enrollment(uuid.uuid4(), course_id)),
        _message(topic_for("workflow.failed.v1"), workflow_failure("run-1")),
        _message(DEAD_LETTER_TOPIC, dead_letter()),
    ]
    for message in messages:
        async with postgres.database.session_scope() as session:
            await CONSUMERS[message.topic](session, message)

    async with postgres.database.session_scope() as session:
        for model in (UserFact, CourseFact, EnrollmentFact):
            assert await session.scalar(select(func.count()).select_from(model)) == 1
        assert await session.scalar(select(func.count()).select_from(FailureRecord)) == 2


async def test_a_dead_letter_without_an_id_cannot_be_recorded_idempotently(_db: None) -> None:
    message = _message(DEAD_LETTER_TOPIC, dead_letter(), with_id=False)
    async with postgres.database.session_scope() as session:
        with pytest.raises(NonRetryableError):
            await CONSUMERS[DEAD_LETTER_TOPIC](session, message)
