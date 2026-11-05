"""participant is_finalist

Revision ID: 7c3e1f0a9b2d
Revises: 689a74162f21
Create Date: 2026-11-05 19:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c3e1f0a9b2d'
down_revision: Union[str, Sequence[str], None] = '689a74162f21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('participants', sa.Column('is_finalist', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('participants', 'is_finalist')
