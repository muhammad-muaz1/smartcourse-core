"""Temporal activities on ``course-tq``. Each opens one transaction and hands the
session to the service — the activity is the "route" of a workflow step. Discovered by
the worker through ``ACTIVITIES`` (platform_core.routing.discover_attribute).
"""

import uuid
from dataclasses import dataclass

from temporalio import activity
from temporalio.exceptions import ApplicationError

from src.db.postgres import database
from src.modules.courses.service import publishing
from src.modules.courses.service.publishing import PublishRunNotFoundError

# Activity failure types the workflow must not retry: retrying cannot change the answer.
VALIDATION_FAILED = "CourseValidationFailed"
RUN_NOT_FOUND = "PublishRunNotFound"


@dataclass(frozen=True)
class PublishRunInput:
    run_id: uuid.UUID


@dataclass(frozen=True)
class PublishFailureInput:
    run_id: uuid.UUID
    failed_step: str
    error: str


def _missing_run(run_id: uuid.UUID) -> ApplicationError:
    return ApplicationError(
        f"publish run {run_id} not found", type=RUN_NOT_FOUND, non_retryable=True
    )


@activity.defn(name="course.publish.validate.v1")
async def validate_course(data: PublishRunInput) -> None:
    try:
        async with database.session_scope() as session:
            problems = await publishing.validate_for_publish(session, data.run_id)
    except PublishRunNotFoundError as exc:
        raise _missing_run(data.run_id) from exc
    # Raised after the transaction commits, so the run keeps its "running" progress
    # until fail_publish records the failure.
    if problems:
        raise ApplicationError(
            "; ".join(problems), problems, type=VALIDATION_FAILED, non_retryable=True
        )


@activity.defn(name="course.publish.mark_published.v1")
async def mark_published(data: PublishRunInput) -> None:
    try:
        async with database.session_scope() as session:
            await publishing.complete_publish(session, data.run_id)
    except PublishRunNotFoundError as exc:
        raise _missing_run(data.run_id) from exc


@activity.defn(name="course.publish.mark_failed.v1")
async def mark_failed(data: PublishFailureInput) -> None:
    try:
        async with database.session_scope() as session:
            await publishing.fail_publish(
                session, data.run_id, failed_step=data.failed_step, error=data.error
            )
    except PublishRunNotFoundError as exc:
        raise _missing_run(data.run_id) from exc


ACTIVITIES = [validate_course, mark_published, mark_failed]
