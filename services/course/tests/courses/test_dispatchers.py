"""Outbox topic → Temporal workflow start, with a recording stand-in for the client."""

import uuid
from datetime import UTC, datetime
from typing import Any

from temporalio.exceptions import WorkflowAlreadyStartedError

from smartcourse_contracts.course_events import CourseEventV1, CourseSnapshotV1
from src.db.outbox import OutboxMessage
from src.modules.courses.dispatchers import OUTBOX_DISPATCHERS, DispatchContext
from src.modules.courses.events import PUBLISH_REQUESTED_TOPIC, topic_for


class RecordingClient:
    def __init__(self, *, already_started: bool = False) -> None:
        self.starts: list[dict[str, Any]] = []
        self.already_started = already_started

    async def start_workflow(self, workflow: Any, arg: Any, **kwargs: Any) -> None:
        if self.already_started:
            raise WorkflowAlreadyStartedError(kwargs["id"], "type")
        self.starts.append({"workflow": workflow, "arg": arg, **kwargs})


def _context(client: RecordingClient) -> DispatchContext:
    return DispatchContext(client=client, task_queue="course-tq")  # type: ignore[arg-type]


def _event_message() -> OutboxMessage:
    event = CourseEventV1(
        event_id=uuid.uuid4(),
        event_type="course.published.v1",
        occurred_at=datetime.now(tz=UTC),
        course=CourseSnapshotV1(
            course_id=uuid.uuid4(),
            revision=3,
            title="t",
            status="published",
            instructor_id=uuid.uuid4(),
        ),
    )
    return OutboxMessage(
        id=event.event_id,
        topic=topic_for("course.published.v1"),
        payload=event.model_dump(mode="json"),
    )


async def test_publish_requested_starts_the_publish_workflow_keyed_by_run() -> None:
    client = RecordingClient()
    run_id = uuid.uuid4()
    message = OutboxMessage(topic=PUBLISH_REQUESTED_TOPIC, payload={"run_id": str(run_id)})

    await OUTBOX_DISPATCHERS[PUBLISH_REQUESTED_TOPIC](_context(client), message)

    assert client.starts[0]["id"] == f"publish-run-{run_id}"
    assert client.starts[0]["arg"].run_id == run_id
    assert client.starts[0]["task_queue"] == "course-tq"


async def test_course_events_start_one_delivery_workflow_per_event() -> None:
    client = RecordingClient()
    message = _event_message()

    await OUTBOX_DISPATCHERS[message.topic](_context(client), message)

    assert client.starts[0]["id"] == f"course-event-{message.id}"
    assert client.starts[0]["arg"].course.revision == 3


async def test_an_already_started_workflow_counts_as_delivered() -> None:
    """The relay crashed after starting but before stamping published_at; redelivery
    must not fail the row forever."""
    client = RecordingClient(already_started=True)
    message = _event_message()

    await OUTBOX_DISPATCHERS[message.topic](_context(client), message)  # no exception


def test_every_course_event_type_has_a_dispatcher() -> None:
    for event_type in ("created", "updated", "published", "unpublished", "archived"):
        assert topic_for(f"course.{event_type}.v1") in OUTBOX_DISPATCHERS  # type: ignore[arg-type]
