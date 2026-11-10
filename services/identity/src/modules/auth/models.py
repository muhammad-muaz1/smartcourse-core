"""SQLAlchemy model for refresh tokens. Stored hashed (SHA-256 — the token itself is
256 bits of random entropy already, so a slow password-style hash buys nothing and
would make every refresh request pay Argon2 cost for no reason).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base, TimestampMixin, UUIDMixin


class RefreshToken(Base, UUIDMixin, TimestampMixin):
    """One row per issued refresh token. `family_id` is shared by every token in one
    rotation chain. A token whose `revoked_at` is already set — because it was rotated
    away or its family was revoked — being presented again means someone is replaying a
    token that should no longer exist: that's the reuse-detection signal, and it
    revokes the whole family.
    """

    __tablename__ = "refresh_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    family_id: Mapped[uuid.UUID] = mapped_column(index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # timezone=True — see the note on db/base.py's TimestampMixin; the same asyncpg
    # naive/aware mismatch applies here.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("refresh_tokens.id", ondelete="SET NULL"), default=None
    )
