"""Импорт участников и выгрузки: /import, /import_teams, /export, /sync, /autoimport."""
import io
import html
from datetime import datetime
from zoneinfo import ZoneInfo

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Document, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.filters import RoleFilter
from app.config import Settings
from app.db.models import Role
from app.services.audit import log_admin_action
from app.services.exports import bot_status_table
from app.services.importer import ImportAborted, assign_teams
from app.services.sheet_sync import (
    format_import_report,
    format_teams_report,
    import_from_sheet,
    import_rows,
    push_status_to_sheet,
)
from app.services.settings_store import AUTO_IMPORT_ENABLED, get_setting, set_setting
from app.services.sources import SheetsError, parse_csv, rows_to_csv

router = Router(name="admin_import")
router.message.filter(F.chat.type == "private")

MAX_FILE_BYTES = 5 * 1024 * 1024
CSV_HELP = (
    "Пришли CSV-файл с подписью <code>/import</code> (или ответь <code>/import</code> на сообщение с файлом).\n"
    "Нужны колонки как в форме (Full name, Email, Phone, …) и колонка отбора "
    "<code>{selected}</code>. Если в файле только отобранные — подпись <code>/import all</code>."
)


def _document(message: Message) -> Document | None:
    if message.document:
        return message.document
    if message.reply_to_message and message.reply_to_message.document:
        return message.reply_to_message.document
    return None


async def _read_csv(bot: Bot, document: Document) -> list[dict[str, str]]:
    if document.file_size and document.file_size > MAX_FILE_BYTES:
        raise ImportAborted("Файл больше 5 МБ — это точно таблица заявок?")
    buf = io.BytesIO()
    await bot.download(document, destination=buf, timeout=60)
    try:
        return parse_csv(buf.getvalue())
    except ValueError as exc:
        raise ImportAborted(str(exc)) from exc


@router.message(Command("import"), RoleFilter(Role.SUPERADMIN))
async def cmd_import(
    message: Message, command: CommandObject, bot: Bot, session: AsyncSession, settings: Settings
) -> None:
    document = _document(message)
    assume_selected = (command.args or "").strip().lower() == "all"
    try:
        if document:
            rows = await _read_csv(bot, document)
            report = await import_rows(session, settings, rows, assume_selected=assume_selected)
            teams_report = None
            source = f"CSV {document.file_name or ''}".strip()
        elif settings.sheets_enabled:
            await message.answer("⏳ Читаю Google Sheet…")
            report, teams_report = await import_from_sheet(session, settings)
            source = "Google Sheet"
        else:
            await message.answer(CSV_HELP.format(selected=html.escape(settings.sheet_selected_column)))
            return
    except ImportAborted as exc:
        await session.rollback()
        await message.answer(f"❌ Импорт отменён, ничего не записано.\n{html.escape(str(exc))}")
        return
    except SheetsError as exc:
        await session.rollback()
        await message.answer(f"❌ {html.escape(str(exc))}\nБот работает дальше, попробуй позже.")
        return

    await log_admin_action(
        session, message.from_user.id, "import", source, created=report.created, updated=report.updated
    )
    await session.commit()
    text = format_import_report(report)
    if teams_report:
        text += "\n\n" + format_teams_report(teams_report)
    await message.answer(text)


@router.message(Command("import_teams"), RoleFilter(Role.SUPERADMIN))
async def cmd_import_teams(message: Message, bot: Bot, session: AsyncSession, settings: Settings) -> None:
    document = _document(message)
    try:
        if document:
            rows = await _read_csv(bot, document)
        elif settings.sheets_enabled and settings.sheet_teams_worksheet:
            from app.services.sources import SheetsClient

            rows = await SheetsClient(settings).read_rows(settings.sheet_teams_worksheet)
        else:
            await message.answer(
                "Пришли CSV с колонками <code>team_name,email</code> и подписью <code>/import_teams</code>."
            )
            return
        report = await assign_teams(session, rows)
    except (ImportAborted, SheetsError) as exc:
        await session.rollback()
        await message.answer(f"❌ Ничего не записано.\n{html.escape(str(exc))}")
        return
    await log_admin_action(session, message.from_user.id, "import_teams", assigned=report.assigned)
    await session.commit()
    await message.answer(format_teams_report(report))


@router.message(Command("export"), RoleFilter(Role.ADMIN))
async def cmd_export(message: Message, session: AsyncSession, settings: Settings) -> None:
    header, rows = await bot_status_table(session, settings.bot_username, settings.timezone)
    stamp = datetime.now(ZoneInfo(settings.timezone)).strftime("%Y%m%d_%H%M")
    await message.answer_document(
        BufferedInputFile(rows_to_csv(header, rows), filename=f"bot_status_{stamp}.csv"),
        caption=f"📤 Участников: {len(rows)}. Колонка invite_link — персональные ссылки для письма.",
    )
    await log_admin_action(session, message.from_user.id, "export", rows=len(rows))


@router.message(Command("sync"), RoleFilter(Role.ADMIN))
async def cmd_sync(message: Message, session: AsyncSession, settings: Settings) -> None:
    if not settings.sheets_enabled:
        await message.answer("Google Sheet пока не подключён. Выгрузка в файл: /export")
        return
    try:
        count = await push_status_to_sheet(session, settings)
    except SheetsError as exc:
        await message.answer(f"❌ {html.escape(str(exc))}")
        return
    await message.answer(f"✅ Лист «{html.escape(settings.sheet_status_worksheet)}» обновлён: {count} строк.")


@router.message(Command("autoimport"), RoleFilter(Role.SUPERADMIN))
async def cmd_autoimport(message: Message, command: CommandObject, session: AsyncSession, settings: Settings) -> None:
    arg = (command.args or "").strip().lower()
    if arg in {"on", "off"}:
        await set_setting(session, AUTO_IMPORT_ENABLED, arg == "on")
        await log_admin_action(session, message.from_user.id, "autoimport", arg)
    enabled = await get_setting(session, AUTO_IMPORT_ENABLED, False)
    note = "" if settings.sheets_enabled else "\n(Google Sheet не подключён — автоимпорт начнёт работать после подключения.)"
    await message.answer(
        f"Автоимпорт из Google Sheet каждые {settings.auto_import_minutes} мин: "
        f"<b>{'включён' if enabled else 'выключен'}</b>.\n"
        f"Переключить: <code>/autoimport on</code> или <code>/autoimport off</code>{note}"
    )
