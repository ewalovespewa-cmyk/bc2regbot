"""day 2 check-in window 15:00-22:00

Revision ID: a1f3c5e7d902
Revises: f2c8d1e5a7b9
Create Date: 2026-11-15 15:35:00.000000

"""
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1f3c5e7d902'
down_revision: Union[str, Sequence[str], None] = 'f2c8d1e5a7b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

log = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    """Финал 15.11: чекин с 15:00 до 22:00 по Астане (UTC+5) и включён — окно по умолчанию 08:00–13:00 уже прошло."""
    bind = op.get_bind()
    updated = bind.execute(sa.text(
        "UPDATE event_days SET checkin_opens_at = '2026-11-15 15:00:00+05', "
        "checkin_closes_at = '2026-11-15 22:00:00+05', is_checkin_enabled = true "
        "WHERE date = '2026-11-15'"
    )).rowcount
    log.info("Окно чекина 15.11: 15:00–22:00, чекин включён (дней обновлено: %s)", updated)


def downgrade() -> None:
    """Окно не откатываем."""
