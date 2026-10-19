"""Доставка событий outbox в бот игр: POST с HMAC-подписью, ретраи с экспоненциальной паузой.

Формат запроса (контракт описан в README, раздел «Интеграция с ботом игр»):
    POST GAMIFICATION_WEBHOOK_URL
    X-BC-Event-Id: <uuid>          — для идемпотентности на стороне получателя
    X-BC-Timestamp: <unix seconds>
    X-BC-Signature: sha256=<hex HMAC-SHA256(GAMIFICATION_WEBHOOK_SECRET, "<timestamp>.<body>")>
    {"event_id": "...", "type": "checkin.created", "created_at": "...", "data": {...}}
Ответ 2xx — доставлено. Иначе повтор через 10 с, 20 с, 40 с … (не реже раза в час) до суток,
потом событие помечается dead и уходит алерт.
"""
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime, timedelta

import aiohttp
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import OutboundEvent, OutboxStatus
from app.logs import get_logger

log = get_logger(__name__)

BATCH = 20
REQUEST_TIMEOUT = 10
MAX_AGE = timedelta(days=1)
MAX_BACKOFF = 3600


def sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"sha256={mac}"


def event_body(event: OutboundEvent) -> bytes:
    return json.dumps(
        {"event_id": str(event.event_id), "type": event.type, "created_at": event.created_at.isoformat(), "data": event.payload},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(10 * 2 ** max(attempts - 1, 0), MAX_BACKOFF))


async def deliver_pending(
    sm: async_sessionmaker[AsyncSession], settings: Settings, http: aiohttp.ClientSession
) -> tuple[int, list[OutboundEvent]]:
    """Один проход. Возвращает (сколько доставлено, список «умерших» событий для алерта)."""
    if not settings.gamification_webhook_url:
        return 0, []
    now = datetime.now(UTC)
    delivered, dead = 0, []
    async with sm() as session:
        events = (
            await session.scalars(
                select(OutboundEvent)
                .where(OutboundEvent.status == OutboxStatus.PENDING, OutboundEvent.next_attempt_at <= func.now())
                .order_by(OutboundEvent.id)
                .limit(BATCH)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for event in events:
            body = event_body(event)
            ts = str(int(time.time()))
            headers = {
                "Content-Type": "application/json",
                "X-BC-Event-Id": str(event.event_id),
                "X-BC-Timestamp": ts,
                "X-BC-Signature": sign(settings.gamification_webhook_secret, ts, body),
            }
            error = None
            try:
                async with http.post(
                    settings.gamification_webhook_url, data=body, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                ) as resp:
                    if 200 <= resp.status < 300:
                        event.status, event.sent_at, event.last_error = OutboxStatus.SENT, datetime.now(UTC), None
                        delivered += 1
                        continue
                    error = f"HTTP {resp.status}: {(await resp.text())[:200]}"
            except Exception as exc:
                error = repr(exc)[:500]
            event.attempts += 1
            event.last_error = error
            if now - event.created_at > MAX_AGE:
                event.status = OutboxStatus.DEAD
                dead.append(event)
            else:
                event.next_attempt_at = now + backoff(event.attempts)
            log.warning("outbox_delivery_failed", event_id=str(event.event_id), attempts=event.attempts, error=error)
        await session.commit()
    return delivered, dead
