"""Прогон настоящих апдейтов через диспетчер без сети.

FakeSession подменяет HTTP-сессию aiogram: каждый вызов Bot API записывается,
а в ответ возвращается правдоподобный объект (Message для send*, True для остального).
"""
import itertools
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    Contact,
    Message,
    MessageEntity,
    PhotoSize,
    Update,
    User,
)

from app.bot.setup import create_dispatcher

BOT_ID = 123456
_update_ids = itertools.count(1)
_message_ids = itertools.count(1)


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self.fail_for: dict[int, Exception] = {}  # chat_id → исключение на отправку

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None) -> Any:
        self.calls.append(method)
        chat_id = getattr(method, "chat_id", None)
        if chat_id in self.fail_for:
            raise self.fail_for[chat_id]
        returning = getattr(method, "__returning__", None)
        if returning is Message:
            return Message(
                message_id=next(_message_ids) + 10_000,
                date=datetime.now(UTC),
                chat=Chat(id=int(chat_id), type="private" if int(chat_id) > 0 else "supergroup"),
                text=getattr(method, "text", None),
                caption=getattr(method, "caption", None),
            )
        if returning is User:
            return User(id=BOT_ID, is_bot=True, first_name="Bot", username="bc_test_bot")
        return True

    async def close(self) -> None:
        pass

    async def stream_content(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError

    # --- удобные выборки для проверок
    def sent(self, chat_id: int | None = None) -> list[TelegramMethod]:
        return [c for c in self.calls if chat_id is None or getattr(c, "chat_id", None) == chat_id]

    def texts(self, chat_id: int | None = None) -> list[str]:
        return [getattr(c, "text", None) or getattr(c, "caption", None) or "" for c in self.sent(chat_id)]

    def last_text(self, chat_id: int) -> str:
        texts = [t for t in self.texts(chat_id) if t]
        return texts[-1] if texts else ""

    def clear(self) -> None:
        self.calls.clear()


def detach_routers() -> None:
    """Роутеры объявлены на уровне модулей и в проде подключаются к одному диспетчеру.
    Тесты создают диспетчер на каждый тест, поэтому перед этим отвязываем роутеры от прошлого."""
    import importlib
    import pkgutil

    import app.bot.errors
    import app.bot.handlers as handlers_pkg
    from aiogram import Router

    modules = [app.bot.errors] + [
        importlib.import_module(f"{handlers_pkg.__name__}.{m.name}") for m in pkgutil.iter_modules(handlers_pkg.__path__)
    ]
    for module in modules:
        for value in vars(module).values():
            if isinstance(value, Router):
                value._parent_router = None


def make_dispatcher(**kwargs):
    detach_routers()
    return create_dispatcher(**kwargs)


class Harness:
    def __init__(self, *, redis, sessionmaker, settings) -> None:
        self.session = FakeSession()
        self.bot = Bot(f"{BOT_ID}:TEST", session=self.session)
        self.settings = settings
        self.dp = make_dispatcher(bot=self.bot, redis=redis, sessionmaker=sessionmaker, settings=settings)

    async def feed(self, update: Update) -> None:
        await self.dp.feed_update(self.bot, update)

    # --- конструкторы апдейтов
    @staticmethod
    def user(uid: int, username: str | None = None, lang: str = "ru") -> User:
        return User(id=uid, is_bot=False, first_name=f"User{uid}", username=username, language_code=lang)

    def _message(self, user: User, chat: Chat, **fields) -> Message:
        return Message(message_id=next(_message_ids), date=datetime.now(UTC), chat=chat, from_user=user, **fields)

    async def text(self, uid: int, text: str, *, username: str | None = None, lang: str = "ru", update_id: int | None = None) -> None:
        user = self.user(uid, username, lang)
        entities = None
        if text.startswith("/"):
            entities = [MessageEntity(type="bot_command", offset=0, length=len(text.split()[0]))]
        msg = self._message(user, Chat(id=uid, type="private"), text=text, entities=entities)
        await self.feed(Update(update_id=update_id or next(_update_ids), message=msg))

    async def contact(self, uid: int, phone: str, *, contact_user_id: int | None = None, username: str | None = None) -> None:
        user = self.user(uid, username)
        contact = Contact(phone_number=phone, first_name="Me", user_id=uid if contact_user_id is None else contact_user_id)
        msg = self._message(user, Chat(id=uid, type="private"), contact=contact)
        await self.feed(Update(update_id=next(_update_ids), message=msg))

    async def photo(self, uid: int, caption: str | None = None) -> None:
        user = self.user(uid)
        photo = [PhotoSize(file_id="photo-file-id", file_unique_id="u1", width=100, height=100)]
        msg = self._message(user, Chat(id=uid, type="private"), photo=photo, caption=caption)
        await self.feed(Update(update_id=next(_update_ids), message=msg))

    async def group_reply(self, chat_id: int, uid: int, reply_to_message_id: int, text: str) -> None:
        user = self.user(uid)
        chat = Chat(id=chat_id, type="supergroup", title="Admins")
        replied = Message(message_id=reply_to_message_id, date=datetime.now(UTC), chat=chat, from_user=self.user(BOT_ID))
        msg = self._message(user, chat, text=text, reply_to_message=replied)
        await self.feed(Update(update_id=next(_update_ids), message=msg))

    async def callback(self, chat_id: int, uid: int, data: str, message_id: int = 1) -> None:
        chat = Chat(id=chat_id, type="supergroup" if chat_id < 0 else "private")
        message = Message(message_id=message_id, date=datetime.now(UTC), chat=chat, text="…")
        cq = CallbackQuery(id=str(next(_update_ids)), from_user=self.user(uid), chat_instance="ci", data=data, message=message)
        await self.feed(Update(update_id=next(_update_ids), callback_query=cq))
