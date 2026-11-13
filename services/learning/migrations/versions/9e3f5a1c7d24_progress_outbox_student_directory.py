"""progress, completion, outbox, student directory

Week 3: the enrollment aggregate gains progress (lesson_completions, lessons_completed)
and a completed state; course_cache gains the lesson list progress is measured against;
student_directory holds roster names from user.registered.v1; outbox_messages carries
enrollment.*.v1 to Kafka. The duplicate rule widens from "one active" to "one not
withdrawn" — a completed enrollment also blocks a second one (API design §7).

Revision ID: 9e3f5a1c7d24
Revises: 7b41e0c2d8a3
Create Date: 2026-11-13 16:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "9e3f5a1c7d24"
down_revision: Union[str, Sequence[str], None] = "7b41e0c2d8a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # A new enum value can't be used in the transaction that adds it, and the CHECK
    # below uses it — so it is committed on its own first.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE enrollment_status ADD VALUE IF NOT EXISTS 'completed'")

    op.add_column(
        "course_cache",
        sa.Column(
            "lesson_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_course_cache_lesson_ids_is_array"),
        "course_cache",
        "jsonb_typeof(lesson_ids) = 'array'",
    )

    op.add_column("enrollments", sa.Column("student_name", sa.String(length=255), nullable=True))
    op.add_column(
        "enrollments", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "enrollments",
        sa.Column("lessons_completed", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_enrollments_lessons_completed_non_negative"),
        "enrollments",
        "lessons_completed >= 0",
    )
    op.create_check_constraint(
        op.f("ck_enrollments_completed_at_iff_completed"),
        "enrollments",
        "(status = 'completed') = (completed_at IS NOT NULL)",
    )
    op.drop_index(
        "uq_enrollments_active_student_course",
        table_name="enrollments",
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "uq_enrollments_current_student_course",
        "enrollments",
        ["student_id", "course_id"],
        unique=True,
        postgresql_where=sa.text("status <> 'withdrawn'"),
    )

    op.create_table(
        "lesson_completions",
        sa.Column("enrollment_id", sa.Uuid(), nullable=False),
        sa.Column("lesson_id", sa.Uuid(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["enrollment_id"],
            ["enrollments.id"],
            name=op.f("fk_lesson_completions_enrollment_id_enrollments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("enrollment_id", "lesson_id", name=op.f("pk_lesson_completions")),
    )
    op.create_table(
        "student_directory",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_student_directory")),
    )
    op.create_table(
        "outbox_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("topic", sa.String(length=255), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("headers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_messages")),
    )
    op.create_index(
        "ix_outbox_messages_unpublished",
        "outbox_messages",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("published_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_outbox_messages_unpublished",
        table_name="outbox_messages",
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.drop_table("outbox_messages")
    op.drop_table("student_directory")
    op.drop_table("lesson_completions")
    op.drop_index(
        "uq_enrollments_current_student_course",
        table_name="enrollments",
        postgresql_where=sa.text("status <> 'withdrawn'"),
    )
    op.drop_constraint(op.f("ck_enrollments_completed_at_iff_completed"), "enrollments")
    op.drop_constraint(op.f("ck_enrollments_lessons_completed_non_negative"), "enrollments")
    # Completed enrollments have no place in the old two-state model; they go back to
    # active so the old one-active index can be rebuilt over them.
    op.execute("UPDATE enrollments SET status = 'active' WHERE status = 'completed'")
    op.create_index(
        "uq_enrollments_active_student_course",
        "enrollments",
        ["student_id", "course_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.drop_column("enrollments", "lessons_completed")
    op.drop_column("enrollments", "completed_at")
    op.drop_column("enrollments", "student_name")
    op.drop_constraint(op.f("ck_course_cache_lesson_ids_is_array"), "course_cache")
    op.drop_column("course_cache", "lesson_ids")
    # Postgres can't drop an enum value; 'completed' stays in the type, unused.
