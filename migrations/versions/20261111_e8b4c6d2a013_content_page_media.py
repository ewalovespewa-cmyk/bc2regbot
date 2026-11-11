"""content page media

Revision ID: e8b4c6d2a013
Revises: d7a3b5c19e02
Create Date: 2026-11-11 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'e8b4c6d2a013'
down_revision: Union[str, Sequence[str], None] = 'd7a3b5c19e02'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'content_pages',
        sa.Column('media_group', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('content_pages', 'media_group')
