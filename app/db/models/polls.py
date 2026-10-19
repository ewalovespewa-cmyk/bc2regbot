from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, created_at_col, str_enum, updated_at_col


class PollKind(StrEnum):
    ATTENDANCE = "attendance"
    CUSTOM = "custom"


class AnswerSource(StrEnum):
    BOT = "bot"
    CALL = "call"  # результат обзвона, внесённый организатором


class Poll(Base):
    __tablename__ = "polls"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[PollKind] = mapped_column(str_enum(PollKind, "poll_kind"))
    question_ru: Mapped[str] = mapped_column(Text)
    question_en: Mapped[str | None] = mapped_column(Text)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminder_after_hours: Mapped[int | None] = mapped_column(Integer)
    max_reminders: Mapped[int] = mapped_column(Integer, server_default="0")
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=false())
    created_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = created_at_col()

    options: Mapped[list["PollOption"]] = relationship(order_by="PollOption.position")


class PollOption(Base):
    __tablename__ = "poll_options"
    __table_args__ = (UniqueConstraint("poll_id", "code"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    poll_id: Mapped[int] = mapped_column(ForeignKey("polls.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(32))  # yes / no / maybe
    label_ru: Mapped[str] = mapped_column(String(100))
    label_en: Mapped[str | None] = mapped_column(String(100))
    position: Mapped[int] = mapped_column(Integer, server_default="0")
    asks_reason: Mapped[bool] = mapped_column(Boolean, server_default=false())
    # Такой ответ считается «ещё не решил»: человеку продолжают приходить напоминания.
    keeps_reminding: Mapped[bool] = mapped_column(Boolean, server_default=false())


class PollAnswer(Base):
    __tablename__ = "poll_answers"

    poll_id: Mapped[int] = mapped_column(ForeignKey("polls.id", ondelete="CASCADE"), primary_key=True)
    participant_id: Mapped[int] = mapped_column(
        ForeignKey("participants.id", ondelete="CASCADE"), primary_key=True
    )
    option_id: Mapped[int] = mapped_column(ForeignKey("poll_options.id", ondelete="CASCADE"), index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    source: Mapped[AnswerSource] = mapped_column(str_enum(AnswerSource, "answer_source"))
    answered_by: Mapped[int | None] = mapped_column(BigInteger)  # для source=call
    answered_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class PollRecipient(Base):
    """Кому опрос уже отправлен и сколько было напоминаний."""

    __tablename__ = "poll_recipients"

    poll_id: Mapped[int] = mapped_column(ForeignKey("polls.id", ondelete="CASCADE"), primary_key=True)
    participant_id: Mapped[int] = mapped_column(
        ForeignKey("participants.id", ondelete="CASCADE"), primary_key=True
    )
    first_sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reminders_sent: Mapped[int] = mapped_column(Integer, server_default="0")
