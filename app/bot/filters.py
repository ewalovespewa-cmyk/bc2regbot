from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Role
from app.services.roles import get_role, has_role


class RoleFilter(BaseFilter):
    """Пропускает пользователей с ролью не ниже min_role и кладёт её в данные хендлера как `role`."""

    def __init__(self, min_role: Role) -> None:
        self.min_role = min_role

    async def __call__(
        self, event: Message | CallbackQuery, session: AsyncSession, settings: Settings
    ) -> bool | dict:
        if event.from_user is None:
            return False
        role = await get_role(session, settings, event.from_user.id)
        return {"role": role} if has_role(role, self.min_role) else False
