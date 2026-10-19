from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at_col, str_enum, updated_at_col


class BroadcastStatus(StrEnum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    DONE = "done"
    CANCELLED = "cancelled"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    BLOCKED = "blocked"


class MediaType(StrEnum):
    PHOTO = "photo"
    VIDEO = "video"
    DOCUMENT = "document"


class Broadcast(Base):
    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    status: Mapped[BroadcastStatus] = mapped_column(
        str_enum(BroadcastStatus, "broadcast_status"), server_default=BroadcastStatus.DRAFT.value
    )
    text_html: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[MediaType | None] = mapped_column(str_enum(MediaType, "media_type"))
    media_file_id: Mapped[str | None] = mapped_column(String(255))
    buttons: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    segment: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    poll_id: Mapped[int | None] = mapped_column(ForeignKey("polls.id", ondelete="SET NULL"))
    is_template: Mapped[bool] = mapped_column(Boolean, server_default=false())
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    report_sent: Mapped[bool] = mapped_column(Boolean, server_default=false())
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class BroadcastDelivery(Base):
    """Одна строка = один получатель. Уникальность по chat_id гарантирует, что дважды не отправим."""

    __tablename__ = "broadcast_deliveries"
    __table_args__ = (
        UniqueConstraint("broadcast_id", "chat_id"),
        Index("ix_broadcast_deliveries_queue", "broadcast_id", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    broadcast_id: Mapped[int] = mapped_column(ForeignKey("broadcasts.id", ondelete="CASCADE"))
    # Может быть пустым: тестовая отправка и TEST_MODE идут админам, которые не участники.
    participant_id: Mapped[int | None] = mapped_column(ForeignKey("participants.id", ondelete="CASCADE"))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[DeliveryStatus] = mapped_column(
        str_enum(DeliveryStatus, "delivery_status"), server_default=DeliveryStatus.PENDING.value
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
