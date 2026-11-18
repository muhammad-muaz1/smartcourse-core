"""assets: uploaded file metadata (bytes live in object storage)

Week 3+: lesson attachments. One row per uploaded file, keyed to a course and optionally
to a lesson; the bytes live under ``storage_key`` in the storage backend
(modules/assets/storage.py), not in the database.

Revision ID: 7f1b9a3c0d58
Revises: 5d2a8c4e9f10
Create Date: 2026-11-18 12:40:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "7f1b9a3c0d58"
down_revision: Union[str, Sequence[str], None] = "5d2a8c4e9f10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "assets",
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("lesson_id", sa.Uuid(), nullable=True),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("checksum_sha256", sa.String(length=64), nullable=False),
        sa.Column("uploaded_by", sa.Uuid(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["course_id"],
            ["courses.id"],
            name=op.f("fk_assets_course_id_courses"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["lesson_id"],
            ["lessons.id"],
            name=op.f("fk_assets_lesson_id_lessons"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assets")),
        sa.UniqueConstraint("storage_key", name=op.f("uq_assets_storage_key")),
    )
    op.create_index(op.f("ix_assets_course_id"), "assets", ["course_id"], unique=False)
    op.create_index(op.f("ix_assets_lesson_id"), "assets", ["lesson_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_assets_lesson_id"), table_name="assets")
    op.drop_index(op.f("ix_assets_course_id"), table_name="assets")
    op.drop_table("assets")
