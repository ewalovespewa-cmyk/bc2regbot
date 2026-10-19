"""Движок рассылок.

Жизненный цикл: draft → (scheduled) → running → done | cancelled.
При старте сегмент «замораживается» в broadcast_deliveries — одна строка на получателя
(уникальность по chat_id), поэтому никто не получит рассылку дважды, а после рестарта
воркер продолжает с невыполненных строк.

Отправка: не быстрее SEND_RATE сообщений в секунду глобально. 429 → ждём retry_after и
повторяем то же сообщение; 403 → участник заблокировал бота (status=blocked); прочие
ошибки — до MAX_ATTEMPTS попыток с паузой. Перед отправкой строка «захватывается»
(attempts+1, next_attempt_at в будущем) и коммитится: если процесс упадёт, строка вернётся
в работу только через CLAIM_SECONDS. Дубль возможен лишь при падении в миллисекунды между
ответом Telegram и коммитом статуса.
"""
import asyncio
import html
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, insert, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import (
    Admin,
    Broadcast,
    BroadcastDelivery,
    BroadcastStatus,
    DeliveryStatus,
    MediaType,
    Participant,
)
from app.logs import get_logger
from app.services import segments

log = get_logger(__name__)

SEND_RATE = 25  # сообщений в секунду, глобально
BATCH = 50
MAX_ATTEMPTS = 3
CLAIM_SECONDS = 300
RETRY_DELAY_SECONDS = 30
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


class RateLimiter:
    """Простое глобальное ограничение: не чаще одного сообщения раз в 1/rate секунды."""

    def __init__(self, rate: float) -> None:
        self.interval = 1.0 / rate
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next > now:
                await asyncio.sleep(self._next - now)
            self._next = max(now, self._next) + self.interval

    def pause(self, seconds: float) -> None:
        self._next = max(self._next, time.monotonic() + seconds)


limiter = RateLimiter(SEND_RATE)


# ---------------------------------------------------------------- контент


def parse_buttons(text: str) -> list[dict[str, str]]:
    """Строки «Текст | https://ссылка» → [{"text", "url"}]. Неверные строки — ValueError с номером."""
    buttons = []
    for n, line in enumerate(text.strip().splitlines(), start=1):
        if not line.strip():
            continue
        if "|" not in line:
            raise ValueError(f"строка {n}: нужен формат «Текст | https://ссылка»")
        label, url = (part.strip() for part in line.split("|", 1))
        if not label or not url.startswith(("https://", "http://", "tg://")):
            raise ValueError(f"строка {n}: ссылка должна начинаться с https://")
        buttons.append({"text": label[:64], "url": url})
    return buttons


def render_text(text_html: str | None, name: str | None) -> str:
    """Подставляет {name} (имя участника); остальной текст отдаём как есть (HTML Telegram)."""
    text = text_html or ""
    if "{name}" in text:
        first = (name or "").split()[0] if name else ""
        text = text.replace("{name}", html.escape(first) or "друг")
    return text


def build_markup(buttons: list[dict[str, str]], extra_rows: list[list[InlineKeyboardButton]] | None = None):
    rows = [list(r) for r in (extra_rows or [])]
    rows += [[InlineKeyboardButton(text=b["text"], url=b["url"])] for b in buttons]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def send_content(
    bot: Bot, chat_id: int, b: Broadcast, *, name: str | None = None, extra_rows=None
) -> Message:
    text = render_text(b.text_html, name)
    markup = build_markup(b.buttons or [], extra_rows)
    if b.media_type == MediaType.PHOTO:
        return await bot.send_photo(chat_id, b.media_file_id, caption=text or None, reply_markup=markup)
    if b.media_type == MediaType.VIDEO:
        return await bot.send_video(chat_id, b.media_file_id, caption=text or None, reply_markup=markup)
    if b.media_type == MediaType.DOCUMENT:
        return await bot.send_document(chat_id, b.media_file_id, caption=text or None, reply_markup=markup)
    return await bot.send_message(chat_id, text, reply_markup=markup)


async def poll_rows(session: AsyncSession, b: Broadcast, lang: str):
    if not b.poll_id:
        return None
    from app.services.polls import poll_keyboard_rows

    return await poll_keyboard_rows(session, b.poll_id, lang)


# ---------------------------------------------------------------- запуск, расписание, отмена


async def test_recipients(session: AsyncSession, settings: Settings) -> list[int]:
    admins = (await session.scalars(select(Admin.telegram_user_id))).all()
    return sorted(set(settings.superadmin_ids) | set(admins))


async def start_broadcast(session: AsyncSession, b: Broadcast, settings: Settings) -> int:
    """Замораживает получателей и переводит рассылку в running. Возвращает число получателей."""
    if b.status not in (BroadcastStatus.DRAFT, BroadcastStatus.SCHEDULED):
        raise ValueError(f"Рассылку в статусе {b.status} нельзя запустить")
    segment = b.segment or {"type": segments.ALL}
    if segment.get("type") == segments.SELF:
        chat_ids = [b.created_by]
    elif settings.test_mode:
        chat_ids = await test_recipients(session, settings)
    else:
        chat_ids = None

    if chat_ids is not None:
        rows = [{"broadcast_id": b.id, "participant_id": None, "chat_id": cid} for cid in chat_ids if cid]
        # Если админ сам участник — привяжем строку к нему (для опросов и подстановки имени).
        by_tg = dict(
            (await session.execute(select(Participant.telegram_user_id, Participant.id).where(
                Participant.telegram_user_id.in_(chat_ids)))).all()
        )
        for row in rows:
            row["participant_id"] = by_tg.get(row["chat_id"])
        if rows:
            await session.execute(insert(BroadcastDelivery).values(rows))
    else:
        stmt = await segments.segment_query(session, segment)
        sub = stmt.subquery()
        await session.execute(
            insert(BroadcastDelivery).from_select(
                ["broadcast_id", "participant_id", "chat_id"],
                select(literal(b.id), sub.c.id, sub.c.telegram_user_id),
            )
        )
    total = await session.scalar(
        select(func.count()).select_from(BroadcastDelivery).where(BroadcastDelivery.broadcast_id == b.id)
    )
    b.status = BroadcastStatus.RUNNING
    b.started_at = datetime.now(UTC)
    b.stats = {**(b.stats or {}), "total": total, "test_mode": settings.test_mode and segment.get("type") != segments.SELF}
    await session.flush()
    log.info("broadcast_started", broadcast_id=b.id, total=total, segment=segment)
    return total


async def cancel_broadcast(session: AsyncSession, b: Broadcast) -> bool:
    if b.status not in (BroadcastStatus.SCHEDULED, BroadcastStatus.RUNNING, BroadcastStatus.DRAFT):
        return False
    b.status = BroadcastStatus.CANCELLED
    b.finished_at = datetime.now(UTC)
    b.stats = {**(b.stats or {}), **await delivery_stats(session, b.id)}
    await session.flush()
    return True


async def start_due_scheduled(session: AsyncSession, settings: Settings) -> list[int]:
    due = (
        await session.scalars(
            select(Broadcast)
            .where(Broadcast.status == BroadcastStatus.SCHEDULED, Broadcast.scheduled_at <= func.now())
            .with_for_update(skip_locked=True)
        )
    ).all()
    started = []
    for b in due:
        await start_broadcast(session, b, settings)
        started.append(b.id)
    return started


async def delivery_stats(session: AsyncSession, broadcast_id: int) -> dict[str, int]:
    rows = await session.execute(
        select(BroadcastDelivery.status, func.count())
        .where(BroadcastDelivery.broadcast_id == broadcast_id)
        .group_by(BroadcastDelivery.status)
    )
    stats = {s.value: 0 for s in DeliveryStatus}
    for status, n in rows:
        stats[status.value] = n
    return stats


# ---------------------------------------------------------------- воркер


@dataclass
class _Row:
    id: int
    chat_id: int
    participant_id: int | None
    name: str | None
    lang: str


async def _claim(sm: async_sessionmaker[AsyncSession], broadcast_id: int) -> list[_Row]:
    now = datetime.now(UTC)
    async with sm() as session:
        result = await session.execute(
            select(
                BroadcastDelivery.id,
                BroadcastDelivery.chat_id,
                BroadcastDelivery.participant_id,
                Participant.full_name,
                Participant.lang,
            )
            .outerjoin(Participant, Participant.id == BroadcastDelivery.participant_id)
            .where(
                BroadcastDelivery.broadcast_id == broadcast_id,
                BroadcastDelivery.status == DeliveryStatus.PENDING,
                or_(BroadcastDelivery.next_attempt_at.is_(None), BroadcastDelivery.next_attempt_at <= now),
            )
            .order_by(BroadcastDelivery.id)
            .limit(BATCH)
            .with_for_update(of=BroadcastDelivery, skip_locked=True)
        )
        rows = [_Row(r[0], r[1], r[2], r[3], r[4] or "ru") for r in result]
        if rows:
            await session.execute(
                update(BroadcastDelivery)
                .where(BroadcastDelivery.id.in_([r.id for r in rows]))
                .values(attempts=BroadcastDelivery.attempts + 1, next_attempt_at=now + timedelta(seconds=CLAIM_SECONDS))
            )
        await session.commit()
    return rows


async def _send_with_retry_after(bot: Bot, row: _Row, b: Broadcast, extra_rows) -> Message:
    for _ in range(5):
        await limiter.wait()
        try:
            return await send_content(bot, row.chat_id, b, name=row.name, extra_rows=extra_rows)
        except TelegramRetryAfter as exc:
            log.warning("telegram_429", retry_after=exc.retry_after, broadcast_id=b.id)
            limiter.pause(exc.retry_after)
            await asyncio.sleep(exc.retry_after)
    raise RuntimeError("Telegram 429 не отпускает")


async def _record(sm, row: _Row, **values: Any) -> None:
    async with sm() as session:
        await session.execute(update(BroadcastDelivery).where(BroadcastDelivery.id == row.id).values(**values))
        if values.get("status") == DeliveryStatus.BLOCKED and row.participant_id:
            await session.execute(
                update(Participant).where(Participant.id == row.participant_id).values(is_blocked_bot=True)
            )
        await session.commit()


async def process_batch(bot: Bot, sm: async_sessionmaker[AsyncSession], broadcast_id: int) -> int:
    """Отправляет одну пачку. Возвращает, сколько строк обработано (0 — нечего делать)."""
    rows = await _claim(sm, broadcast_id)
    if not rows:
        return 0
    async with sm() as session:
        b = await session.get(Broadcast, broadcast_id)
        markups = {lang: await poll_rows(session, b, lang) for lang in {r.lang for r in rows}}
    for row in rows:
        async with sm() as session:  # рассылку могли отменить посреди пачки
            status = await session.scalar(select(Broadcast.status).where(Broadcast.id == broadcast_id))
        if status != BroadcastStatus.RUNNING:
            await _record(sm, row, attempts=BroadcastDelivery.attempts - 1, next_attempt_at=None)
            continue
        try:
            msg = await _send_with_retry_after(bot, row, b, markups.get(row.lang))
        except TelegramForbiddenError as exc:
            await _record(sm, row, status=DeliveryStatus.BLOCKED, error=str(exc)[:500])
        except (TelegramBadRequest, TelegramNotFound) as exc:
            # Ошибка в самих данных (чат не найден, битый file_id) — повтор не поможет.
            await _record(sm, row, status=DeliveryStatus.FAILED, error=str(exc)[:500])
        except Exception as exc:
            async with sm() as session:
                attempts = await session.scalar(select(BroadcastDelivery.attempts).where(BroadcastDelivery.id == row.id))
            if attempts >= MAX_ATTEMPTS:
                await _record(sm, row, status=DeliveryStatus.FAILED, error=repr(exc)[:500])
            else:
                await _record(
                    sm,
                    row,
                    error=repr(exc)[:500],
                    next_attempt_at=datetime.now(UTC) + timedelta(seconds=RETRY_DELAY_SECONDS * attempts),
                )
        else:
            await _record(
                sm, row, status=DeliveryStatus.SENT, message_id=msg.message_id, sent_at=datetime.now(UTC), error=None
            )
    return len(rows)


async def finish_if_complete(session: AsyncSession, broadcast_id: int) -> Broadcast | None:
    """Если строк в работе не осталось — помечает done. Возвращает рассылку, если отчёт пора отправить."""
    pending = await session.scalar(
        select(func.count())
        .select_from(BroadcastDelivery)
        .where(BroadcastDelivery.broadcast_id == broadcast_id, BroadcastDelivery.status == DeliveryStatus.PENDING)
    )
    if pending:
        return None
    b = await session.get(Broadcast, broadcast_id, with_for_update=True)
    if b is None or b.status != BroadcastStatus.RUNNING:
        return None
    b.status = BroadcastStatus.DONE
    b.finished_at = datetime.now(UTC)
    b.stats = {**(b.stats or {}), **await delivery_stats(session, broadcast_id)}
    if b.report_sent:
        return None
    b.report_sent = True
    return b


def report_text(b: Broadcast) -> str:
    s = b.stats or {}
    test = "\n🧪 TEST_MODE: отправлено только админам." if s.get("test_mode") else ""
    duration = ""
    if b.started_at and b.finished_at:
        duration = f"\nЗаняло: {int((b.finished_at - b.started_at).total_seconds())} с"
    return (
        f"📊 <b>Рассылка #{b.id} «{html.escape(b.title)}» завершена</b>\n"
        f"Получателей: {s.get('total', 0)}\n"
        f"✅ Отправлено: {s.get('sent', 0)}\n"
        f"🚫 Заблокировали бота: {s.get('blocked', 0)}\n"
        f"❌ Ошибки: {s.get('failed', 0)}{duration}{test}"
    )


async def run_worker_cycle(bot: Bot, sm: async_sessionmaker[AsyncSession], settings: Settings, time_budget: float = 50) -> None:
    """Один проход воркера: запланированные → в работу, затем пачки до опустошения или time_budget."""
    async with sm() as session:
        await start_due_scheduled(session, settings)
        await session.commit()

    deadline = time.monotonic() + time_budget
    while time.monotonic() < deadline:
        async with sm() as session:
            running = (
                await session.scalars(select(Broadcast.id).where(Broadcast.status == BroadcastStatus.RUNNING).order_by(Broadcast.id))
            ).all()
        if not running:
            return
        processed = 0
        for broadcast_id in running:
            processed += await process_batch(bot, sm, broadcast_id)
            async with sm() as session:
                finished = await finish_if_complete(session, broadcast_id)
                await session.commit()
            if finished:
                await _send_report(bot, settings, finished)
        if not processed:
            return  # остались только строки, ждущие повторной попытки


async def _send_report(bot: Bot, settings: Settings, b: Broadcast) -> None:
    text = report_text(b)
    targets = {b.created_by} if b.created_by else set()
    if settings.admin_chat_id:
        targets.add(settings.admin_chat_id)
    for chat_id in targets:
        try:
            await bot.send_message(chat_id, text)
        except Exception as exc:
            log.warning("broadcast_report_failed", chat_id=chat_id, error=repr(exc))
