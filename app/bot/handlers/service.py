"""Служебные команды для настройки."""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from app.config import Settings

router = Router(name="service")


@router.message(Command("chatid"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_chatid(message: Message, settings: Settings) -> None:
    """Показывает ID группы — его нужно вписать в ADMIN_CHAT_ID. Отвечает только суперадминам."""
    if not message.from_user or message.from_user.id not in settings.superadmin_ids:
        return
    configured = "✅ уже указан в ADMIN_CHAT_ID" if settings.admin_chat_id == message.chat.id else "впиши его в ADMIN_CHAT_ID"
    await message.reply(f"ID этого чата: <code>{message.chat.id}</code>\n{configured}")
