from datetime import datetime
from enum import StrEnum

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, Text, false, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, created_at_col, str_enum, updated_at_col


class Role(StrEnum):
    SUPERADMIN = "superadmin"
    ADMIN = "admin"
    VOLUNTEER = "volunteer"


ROLE_RANK = {Role.VOLUNTEER: 1, Role.ADMIN: 2, Role.SUPERADMIN: 3}


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    is_solo: Mapped[bool] = mapped_column(Boolean, server_default=false())
    created_at: Mapped[datetime] = created_at_col()

    members: Mapped[list["Participant"]] = relationship(back_populates="team")


class Participant(Base):
    __tablename__ = "participants"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)  # всегда lowercase
    full_name: Mapped[str] = mapped_column(String(300))
    phone: Mapped[str | None] = mapped_column(String(32), index=True)  # E.164
    phone_raw: Mapped[str | None] = mapped_column(String(64))
    tg_username_form: Mapped[str | None] = mapped_column(String(64), index=True)  # без @, lowercase
    participation_type: Mapped[str | None] = mapped_column(Text)
    university: Mapped[str | None] = mapped_column(Text)
    year_of_study: Mapped[str | None] = mapped_column(Text)
    major: Mapped[str | None] = mapped_column(Text)
    linkedin: Mapped[str | None] = mapped_column(Text)
    zimran_interest: Mapped[str | None] = mapped_column(Text)
    career_interest: Mapped[str | None] = mapped_column(Text)
    aiesec_project: Mapped[str | None] = mapped_column(Text)

    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id", ondelete="SET NULL"), index=True)
    invite_token: Mapped[str] = mapped_column(String(32), unique=True)

    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    telegram_username: Mapped[str | None] = mapped_column(String(64))
    lang: Mapped[str] = mapped_column(String(5), server_default="ru")
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_blocked_bot: Mapped[bool] = mapped_column(Boolean, server_default=false())
    # Сняли с отбора при повторном импорте — запись остаётся, но бот человека не пускает.
    is_selected: Mapped[bool] = mapped_column(Boolean, server_default=true())

    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    # selectin: команда нужна почти везде, где нужен участник (статус, карточка, выгрузки).
    team: Mapped[Team | None] = relationship(back_populates="members", lazy="selectin")


class Admin(Base):
    __tablename__ = "admins"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    role: Mapped[Role] = mapped_column(str_enum(Role, "admin_role"))
    username: Mapped[str | None] = mapped_column(String(64))
    added_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = created_at_col()
