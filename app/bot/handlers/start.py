from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from app.i18n import pick_lang, t

router = Router(name="start")
router.message.filter(F.chat.type == "private")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    lang = pick_lang(message.from_user.language_code if message.from_user else None)
    await message.answer(t("start.hello", lang))
