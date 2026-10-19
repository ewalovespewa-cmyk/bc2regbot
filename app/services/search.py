"""Поиск участника для волонтёров и админов: по фамилии/имени, телефону, email, команде, @username."""
import re

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Participant, Team
from app.services.normalize import normalize_phone

_DIGITS = re.compile(r"\D")


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def search_participants(session: AsyncSession, query: str, limit: int = 10) -> list[Participant]:
    q = query.strip()
    if len(q) < 2:
        return []
    stmt = select(Participant).outerjoin(Team, Team.id == Participant.team_id).where(Participant.is_selected)

    digits = _DIGITS.sub("", q)
    if q.startswith("@"):
        name = q[1:].lower()
        cond = or_(
            Participant.telegram_username.ilike(_like(name), escape="\\"),
            Participant.tg_username_form.ilike(_like(name), escape="\\"),
        )
    elif "@" in q:
        cond = Participant.email.ilike(_like(q.lower()), escape="\\")
    elif len(digits) >= 6 and len(digits) >= len(q.replace(" ", "")) - 4:
        # Похоже на телефон: точное совпадение после нормализации или по последним цифрам.
        exact = normalize_phone(q)
        cond = or_(Participant.phone == exact, Participant.phone.like(f"%{digits[-10:]}%")) if exact else (
            Participant.phone.like(f"%{digits[-10:]}%")
        )
    else:
        words = [w for w in q.split() if w]
        cond = or_(
            and_(*[Participant.full_name.ilike(_like(w), escape="\\") for w in words]),
            and_(Team.name.ilike(_like(q), escape="\\"), Team.is_solo.is_(False)),
            Participant.email.ilike(_like(q.lower()), escape="\\"),
        )
    rows = await session.scalars(stmt.where(cond).order_by(Participant.full_name).limit(limit))
    return list(rows.all())
