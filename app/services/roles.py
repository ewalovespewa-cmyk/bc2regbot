"""Роли: superadmin (из SUPERADMIN_IDS), admin и volunteer (таблица admins)."""
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import ROLE_RANK, Admin, Role


async def get_role(session: AsyncSession, settings: Settings, user_id: int) -> Role | None:
    if user_id in settings.superadmin_ids:
        return Role.SUPERADMIN
    admin = await session.get(Admin, user_id)
    return admin.role if admin else None


def has_role(role: Role | None, min_role: Role) -> bool:
    return role is not None and ROLE_RANK[role] >= ROLE_RANK[min_role]
