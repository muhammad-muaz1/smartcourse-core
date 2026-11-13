"""Outbox topic → Temporal workflow start, with a recording stand-in for the client."""

import uuid
from typing import Any

from temporalio.exceptions import WorkflowAlreadyStartedError

from smartcourse_contracts.topics import topic_for
from src.db.outbox import OutboxMessage
from src.modules.courses.dispatchers import OUTBOX_DISPATCHERS, DispatchContext
from src.modules.courses.events import PUBLISH_REQUESTED_TOPIC


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


async def test_publish_requested_starts_the_publish_workflow_keyed_by_run() -> None:
    client = RecordingClient()
    run_id = uuid.uuid4()
    message = OutboxMessage(topic=PUBLISH_REQUESTED_TOPIC, payload={"run_id": str(run_id)})

    await OUTBOX_DISPATCHERS[PUBLISH_REQUESTED_TOPIC](_context(client), message)

    assert client.starts[0]["id"] == f"publish-run-{run_id}"
    assert client.starts[0]["arg"].run_id == run_id
    assert client.starts[0]["task_queue"] == "course-tq"


async def test_an_already_started_workflow_counts_as_delivered() -> None:
    """The relay crashed after starting but before stamping published_at; redelivery
    must not fail the row forever."""
    client = RecordingClient(already_started=True)
    message = OutboxMessage(topic=PUBLISH_REQUESTED_TOPIC, payload={"run_id": str(uuid.uuid4())})

    await OUTBOX_DISPATCHERS[PUBLISH_REQUESTED_TOPIC](_context(client), message)  # no exception


def test_course_events_are_left_to_kafka() -> None:
    """Only the internal publish-requested topic is claimed; every course.*.v1 event
    falls through to the Kafka publisher (docs/adr/0012)."""
    for event_type in ("created", "updated", "published", "unpublished", "archived"):
        assert topic_for(f"course.{event_type}.v1") not in OUTBOX_DISPATCHERS
