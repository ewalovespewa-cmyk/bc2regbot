"""Периодические задачи этапа 2: автоимпорт из Google Sheet и обратная запись bot_status."""
from app.logs import get_logger
from app.services.settings_store import AUTO_IMPORT_ENABLED, get_setting
from app.services.sheet_sync import import_from_sheet, push_status_to_sheet
from app.workers.runner import Job, JobContext

log = get_logger(__name__)


async def sheet_status_sync(ctx: JobContext) -> None:
    async with ctx.sessionmaker() as session:
        count = await push_status_to_sheet(session, ctx.settings)
    log.info("sheet_status_synced", rows=count)


async def auto_import(ctx: JobContext) -> None:
    async with ctx.sessionmaker() as session:
        if not await get_setting(session, AUTO_IMPORT_ENABLED, False):
            return
        report, _ = await import_from_sheet(session, ctx.settings)
        await session.commit()
    log.info("auto_import_done", created=report.created, updated=report.updated, errors=len(report.errors))
    if (report.created or report.deselected) and ctx.settings.admin_chat_id:
        await ctx.bot.send_message(
            ctx.settings.admin_chat_id,
            f"📥 Автоимпорт: новых {report.created}, снято с отбора {report.deselected}, ошибок {len(report.errors)}.",
        )


def stage2_jobs() -> list[Job]:
    return [
        Job(
            "auto_import",
            auto_import,
            lambda ctx: ctx.settings.auto_import_minutes * 60 if ctx.settings.sheets_enabled else 0,
        ),
        Job(
            "sheet_status_sync",
            sheet_status_sync,
            lambda ctx: ctx.settings.sheet_sync_minutes * 60 if ctx.settings.sheets_enabled else 0,
        ),
    ]
