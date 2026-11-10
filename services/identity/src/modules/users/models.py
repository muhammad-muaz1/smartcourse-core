"""SQLAlchemy model for the system-of-record user account. One role per user — the
brief has three flat roles (student/instructor/admin), not a permission matrix, so a
single enum column is the invariant, enforced by Postgres, not an `if` in application
code.
"""

from enum import StrEnum

from sqlalchemy import Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base, TimestampMixin, UUIDMixin


class UserRole(StrEnum):
    STUDENT = "student"
    INSTRUCTOR = "instructor"
    ADMIN = "admin"


class User(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(
        # values_callable: SQLAlchemy's Enum defaults to storing the Python enum's
        # .name ("STUDENT"), not .value ("student") — verified empirically. Without
        # this, the DB would store uppercase names while JWTs and every response use
        # the lowercase value, a silent mismatch nothing would catch until someone
        # compared the two directly.
        Enum(
            UserRole,
            name="user_role",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=UserRole.STUDENT,
    )
    is_active: Mapped[bool] = mapped_column(default=True)
