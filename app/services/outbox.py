"""Outbox событий для бота игр. Запись — в той же транзакции, что и само изменение;
доставку (POST с HMAC-подписью и ретраями) делает воркер этапа 8."""
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OutboundEvent

PARTICIPANT_LINKED = "participant.linked"
PARTICIPANT_UNLINKED = "participant.unlinked"
CHECKIN_CREATED = "checkin.created"
CHECKIN_CANCELLED = "checkin.cancelled"


def enqueue(session: AsyncSession, event_type: str, payload: dict[str, Any]) -> OutboundEvent:
    event = OutboundEvent(type=event_type, payload=payload)
    session.add(event)
    return event
