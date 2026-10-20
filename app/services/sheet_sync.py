"""Сценарии поверх импорта: из Google Sheet в БД и статусы обратно в лист bot_status."""
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.services.exports import all_tables
from app.services.importer import ImportAborted, ImportReport, TeamsReport, assign_teams, import_applications
from app.services.sources import Row, SheetsClient


async def import_rows(
    session: AsyncSession, settings: Settings, rows: list[Row], *, assume_selected: bool = False
) -> ImportReport:
    return await import_applications(
        session,
        rows,
        column_map=settings.sheet_column_map,
        selected_column=settings.sheet_selected_column,
        selected_values=settings.sheet_selected_values,
        team_column=settings.sheet_team_column,
        assume_selected=assume_selected,
    )


async def import_from_sheet(session: AsyncSession, settings: Settings) -> tuple[ImportReport, TeamsReport | None]:
    client = SheetsClient(settings)
    rows = await client.read_rows(settings.sheet_applications_worksheet)
    if not rows:
        raise ImportAborted(
            f"Лист «{client.last_read_title}» в Google-таблице пустой. Положите туда заявки "
            "(шапка + строки, как в таблице кандидатов) — или назовите лист с заявками «Кандидаты»."
        )
    try:
        report = await import_rows(session, settings, rows)
    except ImportAborted as exc:
        raise ImportAborted(f"Лист «{client.last_read_title}»: {exc}") from exc
    teams_report = None
    if settings.sheet_teams_worksheet:
        teams_report = await assign_teams(session, await client.read_rows(settings.sheet_teams_worksheet))
    return report, teams_report


async def push_status_to_sheet(session: AsyncSession, settings: Settings) -> int:
    """Пишет вкладки «Прошли» и «Чекин <день>» в Google Sheet. Возвращает число отобранных."""
    tables = await all_tables(session, settings.bot_username, settings.timezone, settings.sheet_status_worksheet)
    await SheetsClient(settings).write_tables(tables)
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
    if report.teams_created:
        lines.append(f"Новых команд: {report.teams_created}")
    if report.errors:
        lines.append("\n❌ <b>Ошибки</b> (строки пропущены):\n" + "\n".join(f"• {e}" for e in report.errors))
    if report.warnings:
        shown = report.warnings[:10]
        more = f"\n… и ещё {len(report.warnings) - 10}" if len(report.warnings) > 10 else ""
        lines.append("\n⚠️ <b>Предупреждения</b>:\n" + "\n".join(f"• {w}" for w in shown) + more)
    lines.append("\nСсылки для рассылки: /export")
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
