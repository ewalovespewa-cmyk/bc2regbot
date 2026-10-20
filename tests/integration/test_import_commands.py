"""Команды /import и /export: вызываем хендлеры напрямую с поддельным сообщением."""
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import func, select

from app.bot.handlers import admin_import
from app.config import Settings
from app.db.models import AdminAction, Participant

CSV = b"Full name,Email,Phone,Selected\nAru B,aru@mail.com,87011112233,yes\nNo Pick,no@mail.com,,no\n"


def _message(caption: str = "/import", with_file: bool = True) -> MagicMock:
    msg = MagicMock()
    msg.from_user.id = 42
    msg.document = MagicMock(file_size=len(CSV), file_name="apps.csv") if with_file else None
    msg.reply_to_message = None
    msg.answer = AsyncMock()
    msg.answer_document = AsyncMock()
    return msg


def _bot() -> MagicMock:
    bot = MagicMock()

    async def download(document, destination, timeout):
        destination.write(CSV)

    bot.download = download
    return bot


def _command(args: str | None = None) -> MagicMock:
    return MagicMock(args=args)


async def test_import_csv_document(session):
    settings = Settings(_env_file=None, sheet_id="")
    msg = _message()
    await admin_import.cmd_import(msg, _command(), _bot(), session, settings)

    assert await session.scalar(select(func.count()).select_from(Participant)) == 1
    assert await session.scalar(select(func.count()).select_from(AdminAction)) == 1
    text = msg.answer.await_args.args[0]
    assert "Новых участников: 1" in text and "Не отобраны (пропущены): 1" in text


async def test_import_without_file_and_sheet_explains_how(session):
    msg = _message(with_file=False)
    await admin_import.cmd_import(msg, _command(), _bot(), session, Settings(_env_file=None, sheet_id=""))
    assert "CSV" in msg.answer.await_args.args[0]


async def test_import_aborts_cleanly(session):
    bad = MagicMock()

    async def download(document, destination, timeout):
        destination.write(b"Name,Mail\nA,b\n")

    bad.download = download
    msg = _message()
    await admin_import.cmd_import(msg, _command(), bad, session, Settings(_env_file=None, sheet_id=""))
    assert "ничего не записано" in msg.answer.await_args.args[0]
    assert await session.scalar(select(func.count()).select_from(Participant)) == 0


async def test_export_sends_csv(session):
    settings = Settings(_env_file=None, bot_username="bc_bot")
    await admin_import.cmd_import(_message(), _command(), _bot(), session, settings)
    msg = _message(with_file=False)
    await admin_import.cmd_export(msg, session, settings)
    file = msg.answer_document.await_args.args[0]
    assert file.filename.endswith(".xlsx")
    from app.services.sources import parse_xlsx

    rows = parse_xlsx(file.data)  # первый лист — «Прошли»
    assert rows[0]["Почта"] == "aru@mail.com"
    assert rows[0]["Ссылка-приглашение"].startswith("https://t.me/bc_bot?start=")
