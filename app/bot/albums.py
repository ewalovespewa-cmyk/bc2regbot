"""Альбомы от админа: Telegram присылает их отдельными сообщениями, бот копит их в Redis до кнопки «Готово»."""
import json
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from redis.asyncio import Redis

from app.services.broadcast import ALBUM_LIMIT

ALBUM_TTL = 3600


def media_item(message: Message) -> dict[str, str] | None:
    if message.photo:
        return {"type": "photo", "file_id": message.photo[-1].file_id}
    if message.video:
        return {"type": "video", "file_id": message.video.file_id}
    if message.document:
        return {"type": "document", "file_id": message.document.file_id}
    return None


def _key(prefix: str, user_id: int, group_id: str) -> str:
    return f"{prefix}:album:{user_id}:{group_id}"


async def collect(message: Message, redis: Redis, *, prefix: str, done_callback: str, hint: str) -> None:
    """Запоминает картинку альбома; на первую отвечает подсказкой с кнопкой «Готово» (callback_data = done_callback:<id>)."""
    item = media_item(message)
    if item is None:
        return
    if message.caption:
        item["caption"] = message.html_text
    key = _key(prefix, message.from_user.id, message.media_group_id)
    await redis.rpush(key, json.dumps({"id": message.message_id, **item}))
    await redis.expire(key, ALBUM_TTL)
    if await redis.set(f"{key}:asked", "1", nx=True, ex=ALBUM_TTL):
        button = InlineKeyboardButton(text="✅ Готово", callback_data=f"{done_callback}:{message.media_group_id}")
        await message.answer(hint, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[button]]))


async def take(redis: Redis, *, prefix: str, user_id: int, group_id: str) -> tuple[list[dict[str, Any]], str, str | None]:
    """Забирает накопленный альбом: (элементы по порядку, подпись, ошибка). При ошибке альбом остаётся в Redis."""
    key = _key(prefix, user_id, group_id)
    items = sorted((json.loads(raw) for raw in await redis.lrange(key, 0, -1)), key=lambda i: i["id"])
    if not items:
        return [], "", "Альбом не найден, пришли его ещё раз"
    kinds = {i["type"] for i in items}
    if len(items) > ALBUM_LIMIT:
        return [], "", f"В альбоме больше {ALBUM_LIMIT} файлов, Telegram столько не отправит"
    if "document" in kinds and len(kinds) > 1:
        return [], "", "Файлы нельзя смешивать с фото и видео в одном альбоме"
    await redis.delete(key)
    caption = next((i["caption"] for i in items if i.get("caption")), "")
    return [{"type": i["type"], "file_id": i["file_id"]} for i in items], caption, None
