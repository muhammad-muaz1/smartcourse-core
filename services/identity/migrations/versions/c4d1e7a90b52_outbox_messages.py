"""outbox_messages

Week 3: identity's transactional outbox, so user.registered.v1 commits with the user row
(docs/adr/0012).

Revision ID: c4d1e7a90b52
Revises: a8ef86b23e0c
Create Date: 2026-11-13 16:10:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c4d1e7a90b52"
down_revision: Union[str, Sequence[str], None] = "a8ef86b23e0c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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
