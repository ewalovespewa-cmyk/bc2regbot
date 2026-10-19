import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at_col, str_enum, updated_at_col
from app.db.models.broadcasts import MediaType


class QuestionStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    DEAD = "dead"  # исчерпаны попытки, нужен ручной разбор


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id", ondelete="CASCADE"), index=True)
    text: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[MediaType | None] = mapped_column(str_enum(MediaType, "question_media_type"))
    media_file_id: Mapped[str | None] = mapped_column(String(255))
    admin_chat_message_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    status: Mapped[QuestionStatus] = mapped_column(
        str_enum(QuestionStatus, "question_status"), server_default=QuestionStatus.OPEN.value
    )
    answer_text: Mapped[str | None] = mapped_column(Text)
    answered_by: Mapped[int | None] = mapped_column(BigInteger)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_col()


class ContentPage(Base):
    __tablename__ = "content_pages"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)  # schedule / location / faq / contacts
    lang: Mapped[str] = mapped_column(String(5), primary_key=True)
    text_html: Mapped[str] = mapped_column(Text)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    updated_by: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = updated_at_col()


class AdminAction(Base):
    __tablename__ = "admin_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int] = mapped_column(BigInteger, index=True)
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(200))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = created_at_col()


class SecurityEvent(Base):
    __tablename__ = "security_events"
    __table_args__ = (Index("ix_security_events_kind_created", "kind", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    participant_id: Mapped[int | None] = mapped_column(ForeignKey("participants.id", ondelete="SET NULL"))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = created_at_col()


class OutboundEvent(Base):
    """Outbox: событие для бота геймификации пишется в той же транзакции, что и чекин."""

    __tablename__ = "outbound_events"
    __table_args__ = (Index("ix_outbound_events_queue", "status", "next_attempt_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[OutboxStatus] = mapped_column(
        str_enum(OutboxStatus, "outbox_status"), server_default=OutboxStatus.PENDING.value
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created_at_col()
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Setting(Base):
    """Переключатели рантайма, которые админ меняет без деплоя."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = updated_at_col()
