"""Modules and lessons. Writes need the owning instructor (or an admin) and a course in
draft/failed status — both checked in the service."""

import uuid

from fastapi import APIRouter, Depends, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from platform_core.response import Envelope, success
from src.core.dependencies import CurrentUser, get_current_user
from src.db.postgres import get_session
from src.modules.courses.schemas import (
    LessonCreate,
    LessonOrder,
    LessonPublic,
    LessonSummary,
    LessonUpdate,
    ModuleCreate,
    ModuleOrder,
    ModulePublic,
    ModuleUpdate,
)
from src.modules.courses.service import courses
from src.modules.courses.service import structure as service

router = APIRouter(tags=["structure"])


def _module_public(module: object) -> ModulePublic:
    # A single module write returns the module without its lessons; read the course
    # or module list for the full tree.
    return ModulePublic.model_validate(module, from_attributes=True)


# --- modules -----------------------------------------------------------------------


@router.post(
    "/courses/{course_id}/modules",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[ModulePublic],
)
async def add_module_route(
    course_id: uuid.UUID,
    body: ModuleCreate,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Envelope[ModulePublic]:
    module = await service.add_module(
        session,
        course_id,
        current_user_id=current_user.id,
        role=current_user.role,
        title=body.title,
        description=body.description,
    )
    return success(_module_public(module))


@router.get("/courses/{course_id}/modules", response_model=Envelope[list[ModulePublic]])
async def list_modules_route(
    course_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> Envelope[list[ModulePublic]]:
    await courses.get_course_or_404(session, course_id)
    return success(await service.module_tree(session, course_id))


@router.put("/courses/{course_id}/modules/order", response_model=Envelope[list[ModulePublic]])
async def reorder_modules_route(
    course_id: uuid.UUID,
    body: ModuleOrder,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Envelope[list[ModulePublic]]:
    modules = await service.reorder_modules(
        session,
        course_id,
        current_user_id=current_user.id,
        role=current_user.role,
        module_ids=body.module_ids,
    )
    return success(modules)


@router.patch("/modules/{module_id}", response_model=Envelope[ModulePublic])
async def update_module_route(
    module_id: uuid.UUID,
    body: ModuleUpdate,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Envelope[ModulePublic]:
    module = await service.update_module(
        session,
        module_id,
        current_user_id=current_user.id,
        role=current_user.role,
        title=body.title,
        description=body.description,
    )
    return success(_module_public(module))


@router.delete("/modules/{module_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_module_route(
    module_id: uuid.UUID,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await service.delete_module(
        session, module_id, current_user_id=current_user.id, role=current_user.role
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- lessons -----------------------------------------------------------------------


@router.post(
    "/modules/{module_id}/lessons",
    status_code=status.HTTP_201_CREATED,
    response_model=Envelope[LessonPublic],
)
async def add_lesson_route(
    module_id: uuid.UUID,
    body: LessonCreate,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Envelope[LessonPublic]:
    lesson = await service.add_lesson(
        session,
        module_id,
        current_user_id=current_user.id,
        role=current_user.role,
        title=body.title,
        lesson_type=body.lesson_type,
        body=body.body,
        duration_minutes=body.duration_minutes,
    )
    return success(LessonPublic.model_validate(lesson, from_attributes=True))


@router.put("/modules/{module_id}/lessons/order", response_model=Envelope[list[LessonSummary]])
async def reorder_lessons_route(
    module_id: uuid.UUID,
    body: LessonOrder,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Envelope[list[LessonSummary]]:
    lessons = await service.reorder_lessons(
        session,
        module_id,
        current_user_id=current_user.id,
        role=current_user.role,
        lesson_ids=body.lesson_ids,
    )
    return success(
        [LessonSummary.model_validate(lesson, from_attributes=True) for lesson in lessons]
    )


@router.get("/lessons/{lesson_id}", response_model=Envelope[LessonPublic])
async def get_lesson_route(
    lesson_id: uuid.UUID,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Envelope[LessonPublic]:
    lesson = await service.get_lesson(
        session, lesson_id, viewer_id=current_user.id, role=current_user.role
    )
    return success(LessonPublic.model_validate(lesson, from_attributes=True))


@router.patch("/lessons/{lesson_id}", response_model=Envelope[LessonPublic])
async def update_lesson_route(
    lesson_id: uuid.UUID,
    body: LessonUpdate,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Envelope[LessonPublic]:
    lesson = await service.update_lesson(
        session,
        lesson_id,
        current_user_id=current_user.id,
        role=current_user.role,
        title=body.title,
        lesson_type=body.lesson_type,
        body=body.body,
        duration_minutes=body.duration_minutes,
    )
    return success(LessonPublic.model_validate(lesson, from_attributes=True))


@router.delete("/lessons/{lesson_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_lesson_route(
    lesson_id: uuid.UUID,
    current_user: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await service.delete_lesson(
        session, lesson_id, current_user_id=current_user.id, role=current_user.role
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
