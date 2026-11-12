"""Course content structure: modules and the lessons inside them.

Every write locks the owning course row first (``get_course_for_update``), which does
three jobs at once: the ownership check, the "content is editable in this status" check,
and serialising concurrent edits so positions never collide. Positions are dense
(0..n-1) and re-compacted after a delete; the unique constraints on position are
deferred to commit so a reorder can swap rows freely inside the transaction.
"""

import uuid
from collections import defaultdict
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.exceptions import NotFoundError, ValidationError
from src.modules.courses.models import (
    Course,
    CourseModule,
    CourseStatus,
    Lesson,
    LessonType,
)
from src.modules.courses.schemas import (
    CourseDetail,
    CoursePublic,
    LessonSummary,
    ModulePublic,
    validate_lesson_body,
)
from src.modules.courses.service.courses import (
    can_modify,
    ensure_can_modify,
    ensure_content_editable,
    get_course_for_update,
    get_course_or_404,
)


class ModuleNotFoundError(NotFoundError):
    code = "MODULE_NOT_FOUND"
    message = "Module not found"


class LessonNotFoundError(NotFoundError):
    code = "LESSON_NOT_FOUND"
    message = "Lesson not found"


class InvalidOrderError(ValidationError):
    code = "INVALID_ORDER"


# --- reads -------------------------------------------------------------------------


async def _modules_of(session: AsyncSession, course_id: uuid.UUID) -> list[CourseModule]:
    result = await session.execute(
        select(CourseModule)
        .where(CourseModule.course_id == course_id)
        .order_by(CourseModule.position)
    )
    return list(result.scalars())


async def _lessons_of_module(session: AsyncSession, module_id: uuid.UUID) -> list[Lesson]:
    result = await session.execute(
        select(Lesson).where(Lesson.module_id == module_id).order_by(Lesson.position)
    )
    return list(result.scalars())


async def module_tree(session: AsyncSession, course_id: uuid.UUID) -> list[ModulePublic]:
    """Modules in order, each with its lesson summaries (no bodies) in order. Two
    queries regardless of course size."""
    modules = await _modules_of(session, course_id)
    lessons = await session.execute(
        select(Lesson).where(Lesson.course_id == course_id).order_by(Lesson.position)
    )
    by_module: dict[uuid.UUID, list[LessonSummary]] = defaultdict(list)
    for lesson in lessons.scalars():
        by_module[lesson.module_id].append(
            LessonSummary.model_validate(lesson, from_attributes=True)
        )
    return [
        ModulePublic(
            id=module.id,
            course_id=module.course_id,
            title=module.title,
            description=module.description,
            position=module.position,
            lessons=by_module.get(module.id, []),
        )
        for module in modules
    ]


async def get_course_detail(session: AsyncSession, course_id: uuid.UUID) -> CourseDetail:
    course = await get_course_or_404(session, course_id)
    course_fields = CoursePublic.model_validate(course, from_attributes=True).model_dump()
    return CourseDetail(**course_fields, modules=await module_tree(session, course_id))


async def get_lesson(
    session: AsyncSession,
    lesson_id: uuid.UUID,
    *,
    viewer_id: uuid.UUID,
    role: str,
) -> Lesson:
    """Owner and admin see any lesson. Everyone else only lessons of a published
    course. (API design says "enrolled students"; course has no enrollment data yet —
    that gate arrives when it consumes enrollment events. See docs/adr/0011.)"""
    lesson = await session.get(Lesson, lesson_id)
    if lesson is None:
        raise LessonNotFoundError()
    course = await get_course_or_404(session, lesson.course_id)
    if course.status != CourseStatus.PUBLISHED and not can_modify(
        course, current_user_id=viewer_id, role=role
    ):
        # Not 403: an unpublished lesson's existence isn't anyone else's business.
        raise LessonNotFoundError()
    return lesson


# --- module writes -----------------------------------------------------------------


async def _editable_course(
    session: AsyncSession, course_id: uuid.UUID, *, current_user_id: uuid.UUID, role: str
) -> Course:
    course = await get_course_for_update(session, course_id)
    ensure_can_modify(course, current_user_id=current_user_id, role=role, action="edit")
    ensure_content_editable(course)
    return course


async def _module_for_write(
    session: AsyncSession, module_id: uuid.UUID, *, current_user_id: uuid.UUID, role: str
) -> CourseModule:
    module = await session.get(CourseModule, module_id)
    if module is None:
        raise ModuleNotFoundError()
    await _editable_course(session, module.course_id, current_user_id=current_user_id, role=role)
    return module


async def add_module(
    session: AsyncSession,
    course_id: uuid.UUID,
    *,
    current_user_id: uuid.UUID,
    role: str,
    title: str,
    description: str | None,
) -> CourseModule:
    await _editable_course(session, course_id, current_user_id=current_user_id, role=role)
    count = await session.scalar(
        select(func.count()).select_from(CourseModule).where(CourseModule.course_id == course_id)
    )
    module = CourseModule(
        course_id=course_id, title=title, description=description, position=count or 0
    )
    session.add(module)
    await session.flush()
    return module


async def update_module(
    session: AsyncSession,
    module_id: uuid.UUID,
    *,
    current_user_id: uuid.UUID,
    role: str,
    title: str | None,
    description: str | None,
) -> CourseModule:
    module = await _module_for_write(session, module_id, current_user_id=current_user_id, role=role)
    if title is not None:
        module.title = title
    if description is not None:
        module.description = description
    await session.flush()
    return module


async def delete_module(
    session: AsyncSession, module_id: uuid.UUID, *, current_user_id: uuid.UUID, role: str
) -> None:
    module = await _module_for_write(session, module_id, current_user_id=current_user_id, role=role)
    course_id = module.course_id
    await session.delete(module)  # lessons go with it (ON DELETE CASCADE)
    await session.flush()
    _renumber(await _modules_of(session, course_id))
    await session.flush()


async def reorder_modules(
    session: AsyncSession,
    course_id: uuid.UUID,
    *,
    current_user_id: uuid.UUID,
    role: str,
    module_ids: Sequence[uuid.UUID],
) -> list[ModulePublic]:
    await _editable_course(session, course_id, current_user_id=current_user_id, role=role)
    modules = await _modules_of(session, course_id)
    _apply_order(modules, module_ids, noun="module")
    await session.flush()
    return await module_tree(session, course_id)


# --- lesson writes -----------------------------------------------------------------


async def _lesson_for_write(
    session: AsyncSession, lesson_id: uuid.UUID, *, current_user_id: uuid.UUID, role: str
) -> Lesson:
    lesson = await session.get(Lesson, lesson_id)
    if lesson is None:
        raise LessonNotFoundError()
    await _editable_course(session, lesson.course_id, current_user_id=current_user_id, role=role)
    return lesson


async def add_lesson(
    session: AsyncSession,
    module_id: uuid.UUID,
    *,
    current_user_id: uuid.UUID,
    role: str,
    title: str,
    lesson_type: LessonType,
    body: dict[str, object],
    duration_minutes: int | None,
) -> Lesson:
    module = await _module_for_write(session, module_id, current_user_id=current_user_id, role=role)
    count = await session.scalar(
        select(func.count()).select_from(Lesson).where(Lesson.module_id == module_id)
    )
    lesson = Lesson(
        module_id=module.id,
        course_id=module.course_id,
        title=title,
        lesson_type=lesson_type,
        body=body,
        duration_minutes=duration_minutes,
        position=count or 0,
    )
    session.add(lesson)
    await session.flush()
    return lesson


async def update_lesson(
    session: AsyncSession,
    lesson_id: uuid.UUID,
    *,
    current_user_id: uuid.UUID,
    role: str,
    title: str | None,
    lesson_type: LessonType | None,
    body: dict[str, object] | None,
    duration_minutes: int | None,
) -> Lesson:
    lesson = await _lesson_for_write(session, lesson_id, current_user_id=current_user_id, role=role)
    if title is not None:
        lesson.title = title
    if lesson_type is not None:
        lesson.lesson_type = lesson_type
    if body is not None:
        # A body sent alone is validated against the lesson's current type here; one
        # sent with a new lesson_type was already validated by the schema.
        lesson.body = validate_lesson_body(lesson.lesson_type, dict(body))
    if duration_minutes is not None:
        lesson.duration_minutes = duration_minutes
    await session.flush()
    return lesson


async def delete_lesson(
    session: AsyncSession, lesson_id: uuid.UUID, *, current_user_id: uuid.UUID, role: str
) -> None:
    lesson = await _lesson_for_write(session, lesson_id, current_user_id=current_user_id, role=role)
    module_id = lesson.module_id
    await session.delete(lesson)
    await session.flush()
    _renumber(await _lessons_of_module(session, module_id))
    await session.flush()


async def reorder_lessons(
    session: AsyncSession,
    module_id: uuid.UUID,
    *,
    current_user_id: uuid.UUID,
    role: str,
    lesson_ids: Sequence[uuid.UUID],
) -> list[Lesson]:
    await _module_for_write(session, module_id, current_user_id=current_user_id, role=role)
    lessons = await _lessons_of_module(session, module_id)
    _apply_order(lessons, lesson_ids, noun="lesson")
    await session.flush()
    return sorted(lessons, key=lambda lesson: lesson.position)


# --- ordering ----------------------------------------------------------------------


def _renumber(items: Sequence[CourseModule | Lesson]) -> None:
    for position, item in enumerate(items):
        item.position = position


def _apply_order(
    items: Sequence[CourseModule | Lesson],
    ordered_ids: Sequence[uuid.UUID],
    *,
    noun: str,
) -> None:
    """``ordered_ids`` must be an exact permutation of the current items — a partial
    list would leave the order of the missing ones undefined."""
    by_id = {item.id: item for item in items}
    if len(ordered_ids) != len(set(ordered_ids)) or set(ordered_ids) != set(by_id):
        raise InvalidOrderError(
            f"Send every {noun} id of this parent exactly once ({len(by_id)} expected)"
        )
    for position, item_id in enumerate(ordered_ids):
        by_id[item_id].position = position
