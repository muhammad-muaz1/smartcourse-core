"""fact tables, failure records, inbox

First revision of analytics_db: one fact row per user, course and enrollment, plus
failure records, all built from events (docs/adr/0012).

Revision ID: 6a0c2e8b4d17
Revises:
Create Date: 2026-11-13 17:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "6a0c2e8b4d17"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_facts",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_user_facts")),
    )
    op.create_index(op.f("ix_user_facts_role"), "user_facts", ["role"], unique=False)
    op.create_table(
        "course_facts",
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("instructor_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("course_id", name=op.f("pk_course_facts")),
    )
    op.create_index(
        op.f("ix_course_facts_instructor_id"), "course_facts", ["instructor_id"], unique=False
    )
    op.create_index(op.f("ix_course_facts_status"), "course_facts", ["status"], unique=False)
    op.create_table(
        "enrollment_facts",
        sa.Column("enrollment_id", sa.Uuid(), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("student_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("withdrawn_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lessons_total", sa.Integer(), nullable=False),
        sa.Column("lessons_completed", sa.Integer(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("enrollment_id", name=op.f("pk_enrollment_facts")),
    )
    op.create_index(
        "ix_enrollment_facts_enrolled_at", "enrollment_facts", ["enrolled_at"], unique=False
    )
    op.create_index(
        "ix_enrollment_facts_course_id_enrolled_at",
        "enrollment_facts",
        ["course_id", "enrolled_at"],
        unique=False,
    )
    op.create_index(
        "ix_enrollment_facts_student_id", "enrollment_facts", ["student_id"], unique=False
    )
    op.create_table(
        "failure_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("service", sa.String(length=64), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("reference_id", sa.String(length=255), nullable=True),
        sa.Column("step", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_failure_records")),
    )
    op.create_index(
        "ix_failure_records_occurred_at", "failure_records", ["occurred_at"], unique=False
    )
    op.create_table(
        "processed_events",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("consumer_group", sa.String(length=255), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id", "consumer_group", name=op.f("pk_processed_events")),
    )


def downgrade() -> None:
    op.drop_table("processed_events")
    op.drop_index("ix_failure_records_occurred_at", table_name="failure_records")
    op.drop_table("failure_records")
    op.drop_index("ix_enrollment_facts_student_id", table_name="enrollment_facts")
    op.drop_index("ix_enrollment_facts_course_id_enrolled_at", table_name="enrollment_facts")
    op.drop_index("ix_enrollment_facts_enrolled_at", table_name="enrollment_facts")
    op.drop_table("enrollment_facts")
    op.drop_index(op.f("ix_course_facts_status"), table_name="course_facts")
    op.drop_index(op.f("ix_course_facts_instructor_id"), table_name="course_facts")
    op.drop_table("course_facts")
    op.drop_index(op.f("ix_user_facts_role"), table_name="user_facts")
    op.drop_table("user_facts")
