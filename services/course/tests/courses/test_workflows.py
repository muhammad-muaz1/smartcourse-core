"""PublishCourseWorkflow's orchestration and compensation, on Temporal's time-skipping
test server with the activities replaced by recording stubs (same names). The test
server binary is downloaded by temporalio on first use, so these are marked
``temporal``; deselect with ``-m "not temporal"`` on a machine without network."""

import uuid
from collections.abc import AsyncIterator

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from src.modules.courses.activities import (
    VALIDATION_FAILED,
    PublishFailureInput,
    PublishRunInput,
)
from src.modules.courses.workflows import PublishCourseWorkflow

pytestmark = pytest.mark.temporal


@pytest.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping(
        data_converter=pydantic_data_converter
    ) as environment:
        yield environment


class Stubs:
    def __init__(self, *, invalid: bool = False) -> None:
        self.calls: list[str] = []
        self.failure: PublishFailureInput | None = None
        self.invalid = invalid

    def activities(self) -> list[object]:
        @activity.defn(name="course.publish.validate.v1")
        async def validate(_data: PublishRunInput) -> None:
            self.calls.append("validate")
            if self.invalid:
                raise ApplicationError(
                    "course has no modules", type=VALIDATION_FAILED, non_retryable=True
                )

        @activity.defn(name="course.publish.mark_published.v1")
        async def mark_published(_data: PublishRunInput) -> None:
            self.calls.append("mark_published")

        @activity.defn(name="course.publish.mark_failed.v1")
        async def mark_failed(data: PublishFailureInput) -> None:
            self.calls.append("mark_failed")
            self.failure = data

        return [validate, mark_published, mark_failed]


async def _run(env: WorkflowEnvironment, stubs: Stubs) -> None:
    async with Worker(
        env.client,
        task_queue="tq",
        workflows=[PublishCourseWorkflow],
        activities=stubs.activities(),
    ):
        await env.client.execute_workflow(
            PublishCourseWorkflow.run,
            PublishRunInput(run_id=uuid.uuid4()),
            id=str(uuid.uuid4()),
            task_queue="tq",
        )


async def test_happy_path_runs_steps_in_order(env: WorkflowEnvironment) -> None:
    stubs = Stubs()
    await _run(env, stubs)
    assert stubs.calls == ["validate", "mark_published"]


async def test_a_failed_step_compensates_once_and_fails_the_workflow(
    env: WorkflowEnvironment,
) -> None:
    stubs = Stubs(invalid=True)
    with pytest.raises(WorkflowFailureError):
        await _run(env, stubs)
    assert stubs.calls == ["validate", "mark_failed"]  # non-retryable: no retries
    assert stubs.failure is not None
    assert stubs.failure.failed_step == "validate"
    assert stubs.failure.error == "course has no modules"
