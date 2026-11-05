"""Сценарии поверх импорта: из Google Sheet в БД и статусы обратно в лист bot_status."""
import hashlib
import json
import time

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.services.exports import Table, all_tables
from app.services.importer import ImportAborted, ImportReport, TeamsReport, assign_teams, import_applications, mark_finalists
from app.services.settings_store import EVENT_LOCK, get_setting
from app.services.sources import Row, SheetMissing, SheetsClient


async def import_rows(
    session: AsyncSession,
    settings: Settings,
    rows: list[Row],
    *,
    assume_selected: bool = False,
    deselect_missing: bool = False,
) -> ImportReport:
    return await import_applications(
        session,
        rows,
        column_map=settings.sheet_column_map,
        selected_column=settings.sheet_selected_column,
        selected_values=settings.sheet_selected_values,
        team_column=settings.sheet_team_column,
        assume_selected=assume_selected,
        deselect_missing=deselect_missing,
    )


async def import_from_sheet(session: AsyncSession, settings: Settings) -> tuple[ImportReport, TeamsReport | None]:
    client = SheetsClient(settings)
    try:
        rows = await client.read_rows(settings.sheet_applications_worksheet)
    except SheetMissing:
        await client.create_source_tab(settings.sheet_applications_worksheet)
        rows = []
    if not rows:
        raise ImportAborted(
            f"Лист «{client.last_read_title}» в Google-таблице пустой. Положите туда прошедших отбор "
            "(строки в том же формате, что и у кандидатов)."
        )
    try:
        report = await import_rows(
            session,
            settings,
            rows,
            assume_selected=settings.sheet_source_all_selected,
            # В режиме мероприятия строка, случайно удалённая из вкладки, не закрывает человеку вход.
            deselect_missing=settings.sheet_source_all_selected and not await get_setting(session, EVENT_LOCK, False),
        )
    except ImportAborted as exc:
        raise ImportAborted(f"Лист «{client.last_read_title}»: {exc}") from exc
    teams_report = None
    if settings.sheet_teams_worksheet:
        teams_report = await assign_teams(session, await client.read_rows(settings.sheet_teams_worksheet))
    return report, teams_report


async def sync_finalists(session: AsyncSession, settings: Settings) -> tuple[int, list[str]]:
    """Забирает вкладку финалистов (создаёт, если её нет). Пустая вкладка — это нормально: финала ещё не было."""
    name = settings.sheet_finalists_worksheet
    client = SheetsClient(settings)
    try:
        rows = await client.read_rows(name)
    except SheetMissing:
        await client.create_source_tab(name)
        rows = []
    try:
        return await mark_finalists(session, rows)
    except ImportAborted as exc:
        raise ImportAborted(f"Лист «{name}»: {exc}") from exc


REWRITE_EVERY_SECONDS = 1800  # даже без изменений: вкладку могли испортить руками
_last_written: dict[tuple[str, str], tuple[str, float]] = {}  # (таблица, вкладка) → (отпечаток, когда)


def _fingerprint(table: Table) -> str:
    payload = json.dumps([table.header, table.rows, table.row_colors], ensure_ascii=False, default=str)
    return hashlib.sha1(payload.encode()).hexdigest()


async def push_status_to_sheet(session: AsyncSession, settings: Settings, *, force: bool = False) -> int:
    """Пишет вкладки «Статус бота» и «Чекин <день>» в Google Sheet, только изменившиеся (force — все).

    Возвращает число отобранных.
    """
    tables = await all_tables(session, settings.timezone, settings.sheet_status_worksheet)
    now = time.monotonic()
    changed = []
    for table in tables:
        fingerprint, written_at = _last_written.get((settings.sheet_id, table.name), ("", 0.0))
        stale = not written_at or now - written_at >= REWRITE_EVERY_SECONDS
        if force or stale or fingerprint != _fingerprint(table):
            changed.append(table)
    if changed:
        await SheetsClient(settings).write_tables(changed)
        for table in changed:
            _last_written[(settings.sheet_id, table.name)] = (_fingerprint(table), now)
    return len(tables[0].rows)


def format_import_report(report: ImportReport) -> str:
    lines = [
        "📥 <b>Импорт завершён</b>",
        f"Строк в источнике: {report.rows}",
        f"Новых участников: {report.created}",
        f"Обновлено: {report.updated}, без изменений: {report.unchanged}",
        f"Не отобраны (пропущены): {report.not_selected}",
    ]
    if report.duplicates:
        lines.append(f"Повторных заявок (взята последняя): {report.duplicates}")
    if report.deselected:
        lines.append(f"⚠️ Сняты с отбора: {report.deselected} (бот перестанет их пускать)")
    if report.deselect_blocked:
        lines.append(
            f"🛑 Из списка пропало сразу {report.deselect_blocked} человек — похоже на ошибку, доступ НЕ закрыт. "
            "Если это намеренно, уберите их частями или импортируйте файлом."
        )
    if report.teams_created:
        lines.append(f"Новых команд: {report.teams_created}")
    if report.errors:
        lines.append("\n❌ <b>Ошибки</b> (строки пропущены):\n" + "\n".join(f"• {e}" for e in report.errors))
    if report.warnings:
        shown = report.warnings[:10]
        more = f"\n… и ещё {len(report.warnings) - 10}" if len(report.warnings) > 10 else ""
        lines.append("\n⚠️ <b>Предупреждения</b>:\n" + "\n".join(f"• {w}" for w in shown) + more)
    return "\n".join(lines)


def format_teams_report(report: TeamsReport) -> str:
    lines = [
        "👥 <b>Команды назначены</b>",
        f"Строк: {report.rows}, назначено: {report.assigned}, без изменений: {report.unchanged}",
        f"Новых команд: {report.teams_created}",
    ]
    if report.unknown_emails:
        sample = ", ".join(report.unknown_emails[:15])
        more = f" и ещё {len(report.unknown_emails) - 15}" if len(report.unknown_emails) > 15 else ""
        lines.append(f"⚠️ Нет среди участников: {sample}{more}")
    if report.errors:
        lines.append("❌ Ошибки:\n" + "\n".join(f"• {e}" for e in report.errors))
    return "\n".join(lines)
