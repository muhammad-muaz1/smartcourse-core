"""SQLAlchemy model for a course. Modules, lessons, prerequisites and the publishing
workflow (docs/architecture/api-design.md §5-6) are a later slice — this is the
basic-CRUD catalogue only: create, browse, read, update metadata, archive.
"""

import uuid
from enum import StrEnum

from sqlalchemy import Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base, TimestampMixin, UUIDMixin


class CourseLevel(StrEnum):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


class CourseStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class Course(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "courses"

    title: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[str] = mapped_column(Text)
    level: Mapped[CourseLevel] = mapped_column(
        # values_callable: see src/modules/users/models.py — without it SQLAlchemy
        # stores the enum member's .name, not its .value, diverging from every JSON
        # representation of this field.
        Enum(
            CourseLevel,
            name="course_level",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=CourseLevel.BEGINNER,
    )
    status: Mapped[CourseStatus] = mapped_column(
        Enum(
            CourseStatus,
            name="course_status",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=CourseStatus.DRAFT,
    )
    # No FK import of users.models needed — a raw UUID column referencing the users
    # table by name is enough, and keeps this module decoupled from users' model.
    instructor_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
