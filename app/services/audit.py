"""Журнал действий админов: кто, что, когда."""
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AdminAction
from app.logs import get_logger

log = get_logger(__name__)


async def log_admin_action(
    session: AsyncSession, actor_id: int, action: str, target: str | None = None, **payload: Any
) -> None:
    session.add(AdminAction(actor_id=actor_id, action=action, target=target, payload=payload))
    log.info("admin_action", actor_id=actor_id, action=action, target=target, **payload)
