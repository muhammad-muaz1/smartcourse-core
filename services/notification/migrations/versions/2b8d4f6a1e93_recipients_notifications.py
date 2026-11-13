"""recipients, notifications, inbox, outbox

First revision of notification_db (docs/adr/0012).

Revision ID: 2b8d4f6a1e93
Revises:
Create Date: 2026-11-13 17:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "2b8d4f6a1e93"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "recipients",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_recipients")),
    )
    op.create_table(
        "notifications",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "account_welcome", "course_welcome", "course_completed", name="notification_kind"
            ),
            nullable=False,
        ),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "sent", "failed", "skipped", name="delivery_status"),
            nullable=False,
        ),
        sa.Column("source_event_id", sa.Uuid(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
        sa.UniqueConstraint("source_event_id", "kind", name="uq_notifications_source_event_kind"),
    )
    op.create_index(
        "ix_notifications_user_id_created_at",
        "notifications",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_notifications_pending",
        "notifications",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "processed_events",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("consumer_group", sa.String(length=255), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id", "consumer_group", name=op.f("pk_processed_events")),
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
    op.drop_table("processed_events")
    op.drop_index(
        "ix_notifications_pending",
        table_name="notifications",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_index("ix_notifications_user_id_created_at", table_name="notifications")
    op.drop_table("notifications")
    op.drop_table("recipients")
    # op.drop_table() leaves native enum types behind (docs/adr/0008).
    sa.Enum(name="delivery_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="notification_kind").drop(op.get_bind(), checkfirst=True)
