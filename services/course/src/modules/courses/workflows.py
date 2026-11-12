"""Temporal workflows owned by the course service, on ``course-tq``.

``PublishCourseWorkflow`` is the publishing saga (API design §6). Workflow code must be
deterministic: no I/O, no clock, no randomness — all of that happens in activities. The
activity module is imported "passed through" so the sandbox doesn't re-import
SQLAlchemy and friends on every replay.

``DeliverCourseEventWorkflow`` carries one ``course.*.v1`` event into learning's
``course_cache`` by scheduling learning's activity — by name, on learning's queue — so
course never imports learning's code and never holds an HTTP client for it. It is the
week-2 transport behind the outbox; Kafka replaces it without touching the API or the
outbox (docs/adr/0011).
"""

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from smartcourse_contracts.course_events import (
        APPLY_COURSE_EVENT_ACTIVITY,
        LEARNING_TASK_QUEUE,
        CourseEventV1,
    )
    from src.modules.courses.activities import (
        RUN_NOT_FOUND,
        VALIDATION_FAILED,
        PublishFailureInput,
        PublishRunInput,
        mark_failed,
        mark_published,
        validate_course,
    )
    from src.modules.courses.service.publishing import STEP_MARK_PUBLISHED, STEP_VALIDATE

_STEP_TIMEOUT = timedelta(seconds=30)
_STEP_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
    non_retryable_error_types=[VALIDATION_FAILED, RUN_NOT_FOUND],
)
# Compensation must land eventually or the course is stuck in "publishing": retry
# until the database is back, however long that takes.
_COMPENSATION_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(minutes=1),
    non_retryable_error_types=[RUN_NOT_FOUND],
)


def _describe(exc: ActivityError) -> str:
    cause = exc.cause
    if isinstance(cause, ApplicationError):
        return cause.message
    return str(cause or exc)


@workflow.defn(name="course.publish.v1")
class PublishCourseWorkflow:
    def __init__(self) -> None:
        self._step = STEP_VALIDATE

    @workflow.query(name="current_step")
    def current_step(self) -> str:
        return self._step

    @workflow.run
    async def run(self, data: PublishRunInput) -> None:
        try:
            self._step = STEP_VALIDATE
            await workflow.execute_activity(
                validate_course,
                data,
                start_to_close_timeout=_STEP_TIMEOUT,
                retry_policy=_STEP_RETRY,
            )
            self._step = STEP_MARK_PUBLISHED
            await workflow.execute_activity(
                mark_published,
                data,
                start_to_close_timeout=_STEP_TIMEOUT,
                retry_policy=_STEP_RETRY,
            )
        except ActivityError as exc:
            cause = exc.cause
            if isinstance(cause, ApplicationError) and cause.type == RUN_NOT_FOUND:
                raise  # nothing to compensate: the run never committed
            await workflow.execute_activity(
                mark_failed,
                PublishFailureInput(
                    run_id=data.run_id, failed_step=self._step, error=_describe(exc)
                ),
                start_to_close_timeout=_STEP_TIMEOUT,
                retry_policy=_COMPENSATION_RETRY,
            )
            # Fail the workflow too, so Temporal's own history shows the failure and
            # the failing step — the run row is the API view of the same fact.
            raise ApplicationError(
                f"publish failed at step '{self._step}': {_describe(exc)}", non_retryable=True
            ) from exc


@workflow.defn(name="course.deliver_event.v1")
class DeliverCourseEventWorkflow:
    @workflow.run
    async def run(self, event: CourseEventV1) -> None:
        await workflow.execute_activity(
            APPLY_COURSE_EVENT_ACTIVITY,
            event,
            task_queue=LEARNING_TASK_QUEUE,
            start_to_close_timeout=timedelta(seconds=30),
            # Unlimited attempts with capped backoff: if learning is down, its cache
            # catches up when it returns. The event is durable in Temporal meanwhile.
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=1),
                maximum_interval=timedelta(minutes=1),
            ),
        )


WORKFLOWS = [PublishCourseWorkflow, DeliverCourseEventWorkflow]
