"""remove test checkin days

Revision ID: c5e92a7d3b41
Revises: b4d81c2e6f10
Create Date: 2026-11-10 16:00:00.000000

"""
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5e92a7d3b41'
down_revision: Union[str, Sequence[str], None] = 'b4d81c2e6f10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

log = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    """Мероприятие идёт 14 и 15 ноября; любой более ранний день — тестовый. Удаляем его вместе с отметками."""
    bind = op.get_bind()
    condition = "date < '2026-11-14'"
    labels = [row[0] for row in bind.execute(sa.text(f"SELECT label FROM event_days WHERE {condition}"))]
    checkins = bind.execute(sa.text(
        f"DELETE FROM checkins WHERE event_day_id IN (SELECT id FROM event_days WHERE {condition})"
    )).rowcount
    days = bind.execute(sa.text(f"DELETE FROM event_days WHERE {condition}")).rowcount
    log.info("Удалены тестовые дни чекина: %s (дней %s, отметок %s)", ", ".join(labels) or "нет", days, checkins)


def downgrade() -> None:
    """Удалённые тестовые данные не восстанавливаются."""
