from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, String, UniqueConstraint, false, true
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, created_at_col, str_enum


class CheckinMethod(StrEnum):
    SELF_QR = "self_qr"
    VOLUNTEER = "volunteer"
    ADMIN = "admin"


class EventDay(Base):
    __tablename__ = "event_days"

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, unique=True)
    label: Mapped[str] = mapped_column(String(100))
    checkin_opens_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    checkin_closes_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_checkin_enabled: Mapped[bool] = mapped_column(Boolean, server_default=false())
    is_screen_enabled: Mapped[bool] = mapped_column(Boolean, server_default=true())


class Checkin(Base):
    __tablename__ = "checkins"
    __table_args__ = (UniqueConstraint("participant_id", "event_day_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id", ondelete="CASCADE"))
    event_day_id: Mapped[int] = mapped_column(ForeignKey("event_days.id", ondelete="CASCADE"), index=True)
    method: Mapped[CheckinMethod] = mapped_column(str_enum(CheckinMethod, "checkin_method"))
    checked_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = created_at_col()
