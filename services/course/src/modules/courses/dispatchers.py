"""Outbox topics this module delivers somewhere other than Kafka. Discovered by the worker
through ``OUTBOX_DISPATCHERS`` (topic → coroutine); every other topic goes to Kafka.

The workflow id is derived from the run, and a duplicate start is treated as already
delivered — so a relay that crashes between "started" and "marked published" redelivers
harmlessly.
"""

import contextlib
import uuid
from dataclasses import dataclass

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from src.db.outbox import OutboxMessage
from src.modules.courses.activities import PublishRunInput
from src.modules.courses.events import PUBLISH_REQUESTED_TOPIC
from src.modules.courses.service.publishing import workflow_id_for
from src.modules.courses.workflows import PublishCourseWorkflow


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


OUTBOX_DISPATCHERS = {
    PUBLISH_REQUESTED_TOPIC: _start_publish,
}
