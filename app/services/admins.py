"""Управление админами и волонтёрами (таблица admins). Суперадмины — только из SUPERADMIN_IDS."""
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Admin, Participant, Role

USERNAMES_KEY = "users:by_username"
ASSIGNABLE_ROLES = {"admin": Role.ADMIN, "volunteer": Role.VOLUNTEER}


@dataclass
class ResolvedUser:
    id: int
    username: str | None = None


async def remember_user(redis: Redis, user_id: int, username: str | None) -> None:
    """Запоминаем username → id всех, кто писал боту: Bot API не умеет искать людей по @username."""
    if username:
        await redis.hset(USERNAMES_KEY, username.lower(), user_id)


async def resolve_user(session: AsyncSession, redis: Redis, ref: str) -> ResolvedUser | None:
    """«123456789» или «@username» → Telegram ID. Username находим, только если человек писал боту."""
    ref = ref.strip()
    if ref.lstrip("-").isdigit():
        return ResolvedUser(int(ref))
    username = ref.lstrip("@").lower()
    if not username:
        return None
    if user_id := await redis.hget(USERNAMES_KEY, username):
        return ResolvedUser(int(user_id), username)
    if participant := await session.scalar(select(Participant).where(Participant.telegram_username == username)):
        return ResolvedUser(participant.telegram_user_id, username)
    if admin := await session.scalar(select(Admin).where(Admin.username == username)):
        return ResolvedUser(admin.telegram_user_id, username)
    return None


async def set_admin(session: AsyncSession, user: ResolvedUser, role: Role, added_by: int) -> tuple[Admin, Role | None]:
    """Добавляет или меняет роль. Возвращает (запись, прежняя роль)."""
    admin = await session.get(Admin, user.id)
    previous = admin.role if admin else None
    if admin is None:
        admin = Admin(telegram_user_id=user.id, role=role, username=user.username, added_by=added_by)
        session.add(admin)
    else:
        admin.role = role
        admin.added_by = added_by
        if user.username:
            admin.username = user.username
    await session.flush()
    return admin, previous


async def remove_admin(session: AsyncSession, user_id: int) -> Admin | None:
    admin = await session.get(Admin, user_id)
    if admin is not None:
        await session.delete(admin)
        await session.flush()
    return admin


async def list_admins(session: AsyncSession) -> list[Admin]:
    return list((await session.scalars(select(Admin).order_by(Admin.role, Admin.created_at))).all())
