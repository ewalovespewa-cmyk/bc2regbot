"""outbox backlog dead

Revision ID: f2c8d1e5a7b9
Revises: e8b4c6d2a013
Create Date: 2026-11-11 20:00:00.000000

"""
import logging
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2c8d1e5a7b9'
down_revision: Union[str, Sequence[str], None] = 'e8b4c6d2a013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

log = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    """Пока бот игр не был подключён, события копились. Старые (в т.ч. тестовые чекины) не отправляем."""
    count = op.get_bind().execute(sa.text(
        "UPDATE outbound_events SET status = 'dead', last_error = 'накопилось до подключения бота игр' "
        "WHERE status = 'pending'"
    )).rowcount
    log.info("Старые события для бота игр помечены неотправляемыми: %s", count)


def downgrade() -> None:
    """Не восстанавливаем."""
