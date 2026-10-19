"""Периодические задачи: рассылки, напоминания опросов, автоимпорт и bot_status в Google Sheet."""
from app.logs import get_logger
from app.services import broadcast, polls
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


async def broadcasts(ctx: JobContext) -> None:
    await broadcast.run_worker_cycle(ctx.bot, ctx.sessionmaker, ctx.settings, time_budget=50)


async def poll_reminders(ctx: JobContext) -> None:
    async with ctx.sessionmaker() as session:
        for poll in await polls.due_reminders(session):
            b = await polls.send_reminder(session, poll, ctx.settings)
            log.info("poll_reminder", poll_id=poll.id, broadcast_id=b.id if b else None)
        await session.commit()


def all_jobs() -> list[Job]:
    sheets = lambda minutes: (lambda ctx: minutes(ctx) * 60 if ctx.settings.sheets_enabled else 0)  # noqa: E731
    jobs = [
        Job("broadcasts", broadcasts, lambda ctx: 2, timeout=120),
        Job("poll_reminders", poll_reminders, lambda ctx: 60),
        Job("auto_import", auto_import, sheets(lambda ctx: ctx.settings.auto_import_minutes)),
        Job("sheet_status_sync", sheet_status_sync, sheets(lambda ctx: ctx.settings.sheet_sync_minutes)),
    ]
    return jobs
