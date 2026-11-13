"""Every topic and the schema of the messages on it — the registry in "every cross-service
fact is an event with a registered schema" (CLAUDE.md §3).

The committed JSON Schemas under ``libs/contracts/schemas/`` are generated from these
models, and ``tests/test_schemas.py`` fails if a change to a model is not BACKWARD
compatible with its committed schema — the check a Schema Registry would make, run in CI
(docs/adr/0012).
"""

from pydantic import BaseModel

from smartcourse_contracts.course_events import COURSE_TOPICS, CourseEventV1
from smartcourse_contracts.enrollment_events import ENROLLMENT_TOPICS, EnrollmentEventV1
from smartcourse_contracts.platform_events import (
    WORKFLOW_FAILED_TOPIC,
    DeadLetterV1,
    WorkflowFailedEventV1,
)
from smartcourse_contracts.topics import DEAD_LETTER_TOPIC
from smartcourse_contracts.user_events import USER_REGISTERED_TOPIC, UserEventV1

TOPIC_SCHEMAS: dict[str, type[BaseModel]] = {
    **dict.fromkeys(COURSE_TOPICS, CourseEventV1),
    USER_REGISTERED_TOPIC: UserEventV1,
    **dict.fromkeys(ENROLLMENT_TOPICS, EnrollmentEventV1),
    WORKFLOW_FAILED_TOPIC: WorkflowFailedEventV1,
    DEAD_LETTER_TOPIC: DeadLetterV1,
}

ALL_TOPICS: tuple[str, ...] = tuple(TOPIC_SCHEMAS)
