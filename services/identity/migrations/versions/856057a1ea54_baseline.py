"""baseline

Intentionally empty: establishes the alembic_version table and migration chain before
any table exists. The first real schema migration lands with Phase 3's identity models.

Revision ID: 856057a1ea54
Revises:
Create Date: 2026-09-17 19:40:26.407686

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "856057a1ea54"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
