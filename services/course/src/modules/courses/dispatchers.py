"""How the outbox relay delivers this module's outbox topics. Discovered by the relay
through ``OUTBOX_DISPATCHERS`` (topic → coroutine).

Workflow ids are derived from the outbox row, and a duplicate start is treated as
already delivered — so a relay that crashes between "started" and "marked published"
redelivers harmlessly.
"""

import contextlib
import uuid
from dataclasses import dataclass

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from smartcourse_contracts.course_events import CourseEventV1
from src.db.outbox import OutboxMessage
from src.modules.courses.activities import PublishRunInput
from src.modules.courses.events import PUBLISH_REQUESTED_TOPIC, topic_for
from src.modules.courses.service.publishing import workflow_id_for
from src.modules.courses.workflows import DeliverCourseEventWorkflow, PublishCourseWorkflow


@dataclass(frozen=True, slots=True)
class DispatchContext:
    client: Client
    task_queue: str


async def _start_publish(context: DispatchContext, message: OutboxMessage) -> None:
    run_id = uuid.UUID(message.payload["run_id"])
    with contextlib.suppress(WorkflowAlreadyStartedError):
        await context.client.start_workflow(
            PublishCourseWorkflow.run,
            PublishRunInput(run_id=run_id),
            id=workflow_id_for(run_id),
            task_queue=context.task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )


async def _deliver_course_event(context: DispatchContext, message: OutboxMessage) -> None:
    event = CourseEventV1.model_validate(message.payload)
    with contextlib.suppress(WorkflowAlreadyStartedError):
        await context.client.start_workflow(
            DeliverCourseEventWorkflow.run,
            event,
            id=f"course-event-{event.event_id}",
            task_queue=context.task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )


OUTBOX_DISPATCHERS = {
    PUBLISH_REQUESTED_TOPIC: _start_publish,
    topic_for("course.created.v1"): _deliver_course_event,
    topic_for("course.updated.v1"): _deliver_course_event,
    topic_for("course.published.v1"): _deliver_course_event,
    topic_for("course.unpublished.v1"): _deliver_course_event,
    topic_for("course.archived.v1"): _deliver_course_event,
}
