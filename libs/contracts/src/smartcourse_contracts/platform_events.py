"""Platform-wide failure facts, produced by any service, consumed by analytics' "Failed
Events / Workflow Issues" metric (API design §10).

- ``workflow.failed.v1``: a background workflow or task gave up — a publish saga that
  compensated, a notification that exhausted its retries. Written to the producer's
  outbox in the same transaction that records the failure.
- the dead-letter topic: a consumer could not handle a message after its retries and
  parked it rather than block its partition. Produced by the consumer runner directly
  (there is no business transaction to attach it to); ``payload`` is the original
  message, verbatim, for replay.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from smartcourse_contracts.topics import topic_for

WorkflowEventType = Literal["workflow.failed.v1"]
WORKFLOW_FAILED_TOPIC = topic_for("workflow.failed.v1")


class WorkflowFailureV1(BaseModel):
    # Which service and which kind of work: e.g. "course" / "course.publish".
    service: str
    workflow: str
    # The id of the thing the work was about (a publish run, a notification).
    reference_id: str
    failed_step: str | None = None
    error: str


class WorkflowFailedEventV1(BaseModel):
    event_id: uuid.UUID
    event_type: WorkflowEventType
    occurred_at: datetime
    failure: WorkflowFailureV1


class DeadLetterV1(BaseModel):
    event_id: uuid.UUID | None = None
    original_topic: str
    partition: int
    offset: int
    consumer_group: str
    attempts: int
    error: str
    failed_at: datetime
    # The original message value, decoded as text (invalid UTF-8 replaced).
    payload: str
