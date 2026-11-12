"""Builders shared by the courses tests — a course with content, ready to publish."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.courses.models import Course, CourseLevel, LessonType
from src.modules.courses.service import courses, structure

INSTRUCTOR = "instructor"


async def make_course(
    session: AsyncSession, owner: uuid.UUID, *, modules: int = 0, lessons_each: int = 1
) -> Course:
    course = await courses.create_course(
        session,
        title="Distributed Systems",
        description="d",
        level=CourseLevel.ADVANCED,
        instructor_id=owner,
        max_enrollments=10,
    )
    for m in range(modules):
        module = await structure.add_module(
            session,
            course.id,
            current_user_id=owner,
            role=INSTRUCTOR,
            title=f"M{m}",
            description=None,
        )
        for n in range(lessons_each):
            await structure.add_lesson(
                session,
                module.id,
                current_user_id=owner,
                role=INSTRUCTOR,
                title=f"L{m}.{n}",
                lesson_type=LessonType.TEXT,
                body={"content": "hello"},
                duration_minutes=5,
            )
    return course
