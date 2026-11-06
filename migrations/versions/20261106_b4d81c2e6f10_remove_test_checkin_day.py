"""remove test checkin day 05.11

Revision ID: b4d81c2e6f10
Revises: 7c3e1f0a9b2d
Create Date: 2026-11-06 12:00:00.000000

"""
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4d81c2e6f10'
down_revision: Union[str, Sequence[str], None] = '7c3e1f0a9b2d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

log = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    """Тестовый день 05.11 попал в «Мой статус» и таблицу. Настоящие дни (14 и 15 ноября) не затрагиваются."""
    bind = op.get_bind()
    condition = "label = '05.11' AND date < '2026-11-14'"
    checkins = bind.execute(sa.text(
        f"DELETE FROM checkins WHERE event_day_id IN (SELECT id FROM event_days WHERE {condition})"
    )).rowcount
    days = bind.execute(sa.text(f"DELETE FROM event_days WHERE {condition}")).rowcount
    log.info("Удалён тестовый день 05.11: дней %s, отметок %s", days, checkins)


def downgrade() -> None:
    """Удалённые тестовые данные не восстанавливаются."""
