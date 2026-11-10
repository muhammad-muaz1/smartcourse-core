"""create courses table

First revision of course_db. Same shape as the monolith's courses table, minus the
foreign key to users: users now live in identity_db, and cross-service references are
by UUID only.

Revision ID: 3c9e1f0a7b21
Revises:
Create Date: 2026-09-28 18:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "3c9e1f0a7b21"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "courses",
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "level",
            sa.Enum("beginner", "intermediate", "advanced", name="course_level"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("draft", "published", "archived", name="course_status"),
            nullable=False,
        ),
        sa.Column("instructor_id", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_courses")),
    )
    op.create_index(op.f("ix_courses_instructor_id"), "courses", ["instructor_id"], unique=False)
    op.create_index(op.f("ix_courses_title"), "courses", ["title"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_courses_title"), table_name="courses")
    op.drop_index(op.f("ix_courses_instructor_id"), table_name="courses")
    op.drop_table("courses")
    # op.drop_table() does not drop the Postgres native enum types it depended on
    # (docs/adr/0008) — left behind, they collide with the next upgrade's CREATE TYPE.
    sa.Enum(name="course_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="course_level").drop(op.get_bind(), checkfirst=True)
