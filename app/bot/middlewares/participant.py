"""Кладёт в данные хендлера `participant` (привязанный и отобранный, иначе None) и `lang`."""
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.i18n import pick_lang
from app.services.admins import remember_user
from app.services.linking import get_participant_by_tg


class ParticipantMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        participant = None
        user = event.from_user if isinstance(event, (Message, CallbackQuery)) else None
        if user is not None:
            participant = await get_participant_by_tg(data["session"], user.id)
            if user.username and "redis" in data:
                await remember_user(data["redis"], user.id, user.username)
        data["participant"] = participant
        data["lang"] = participant.lang if participant else pick_lang(user.language_code if user else None)
        return await handler(event, data)
