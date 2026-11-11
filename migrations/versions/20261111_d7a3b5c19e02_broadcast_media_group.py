"""broadcast media group

Revision ID: d7a3b5c19e02
Revises: c5e92a7d3b41
Create Date: 2026-11-11 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'd7a3b5c19e02'
down_revision: Union[str, Sequence[str], None] = 'c5e92a7d3b41'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'broadcasts',
        sa.Column('media_group', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('broadcasts', 'media_group')
