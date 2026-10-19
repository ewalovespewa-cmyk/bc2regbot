"""Привязка Telegram-аккаунта к записи участника: по токену из ссылки или по номеру из контакта.

Правила:
- ссылку, уже привязанную к другому аккаунту, не перепривязываем;
- аккаунт, уже привязанный к другому участнику, ко второй записи не привязываем;
- снятые с отбора (is_selected=False) считаются «не найден».
Строка участника блокируется (SELECT … FOR UPDATE), так что две одновременные попытки
привязать одну ссылку с разных аккаунтов не пройдут обе.
"""
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Participant
from app.i18n import pick_lang
from app.services import outbox
from app.services.importer import team_display_name


class LinkStatus(StrEnum):
    LINKED = "linked"
    ALREADY_LINKED = "already_linked"
    NOT_FOUND = "not_found"
    TOKEN_TAKEN = "token_taken"  # запись уже привязана к другому Telegram
    ACCOUNT_TAKEN = "account_taken"  # этот Telegram уже привязан к другому участнику
    AMBIGUOUS = "ambiguous"  # по номеру нашлось несколько свободных записей


@dataclass
class TelegramUser:
    id: int
    username: str | None = None
    language_code: str | None = None


@dataclass
class LinkResult:
    status: LinkStatus
    participant: Participant | None = None
    other: Participant | None = None

    @property
    def ok(self) -> bool:
        return self.status in (LinkStatus.LINKED, LinkStatus.ALREADY_LINKED)


async def get_participant_by_tg(session: AsyncSession, telegram_user_id: int) -> Participant | None:
    return await session.scalar(
        select(Participant).where(Participant.telegram_user_id == telegram_user_id, Participant.is_selected)
    )


async def _link(session: AsyncSession, participant: Participant, user: TelegramUser) -> LinkResult:
    username = user.username.lower() if user.username else None
    if participant.telegram_user_id == user.id:
        if participant.telegram_username != username:
            participant.telegram_username = username
        participant.is_blocked_bot = False
        return LinkResult(LinkStatus.ALREADY_LINKED, participant)
    if participant.telegram_user_id is not None:
        return LinkResult(LinkStatus.TOKEN_TAKEN, participant)

    other = await session.scalar(select(Participant).where(Participant.telegram_user_id == user.id))
    if other is not None:
        return LinkResult(LinkStatus.ACCOUNT_TAKEN, participant, other)

    participant.telegram_user_id = user.id
    participant.telegram_username = username
    participant.linked_at = datetime.now(UTC)
    participant.lang = pick_lang(user.language_code)
    participant.is_blocked_bot = False
    outbox.enqueue(
        session,
        outbox.PARTICIPANT_LINKED,
        {
            "telegram_user_id": user.id,
            "participant_id": participant.id,
            "full_name": participant.full_name,
            "team": team_display_name(participant.team, "en"),
            "linked_at": participant.linked_at.isoformat(),
        },
    )
    await session.flush()
    return LinkResult(LinkStatus.LINKED, participant)


async def link_by_token(session: AsyncSession, user: TelegramUser, token: str) -> LinkResult:
    participant = await session.scalar(
        select(Participant).where(Participant.invite_token == token).with_for_update()
    )
    if participant is None or not participant.is_selected:
        return LinkResult(LinkStatus.NOT_FOUND)
    return await _link(session, participant, user)


async def link_by_contact(
    session: AsyncSession, user: TelegramUser, phone: str | None
) -> LinkResult:
    """Ищет по номеру из контакта; если не нашли — по Telegram username из формы."""
    candidates: list[Participant] = []
    if phone:
        candidates = list(
            (
                await session.scalars(
                    select(Participant)
                    .where(Participant.phone == phone, Participant.is_selected)
                    .order_by(Participant.id)
                    .with_for_update()
                )
            ).all()
        )
    if not candidates and user.username:
        candidates = list(
            (
                await session.scalars(
                    select(Participant)
                    .where(Participant.tg_username_form == user.username.lower(), Participant.is_selected)
                    .order_by(Participant.id)
                    .with_for_update()
                )
            ).all()
        )
    if not candidates:
        return LinkResult(LinkStatus.NOT_FOUND)

    mine = [p for p in candidates if p.telegram_user_id == user.id]
    if mine:
        return await _link(session, mine[0], user)
    free = [p for p in candidates if p.telegram_user_id is None]
    if len(free) > 1:
        return LinkResult(LinkStatus.AMBIGUOUS, free[0])
    if not free:
        return LinkResult(LinkStatus.TOKEN_TAKEN, candidates[0])
    return await _link(session, free[0], user)
