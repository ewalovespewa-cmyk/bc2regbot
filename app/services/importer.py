"""Импорт отобранных участников из таблицы заявок.

Идемпотентен: ключ — email в нижнем регистре. Повторный импорт обновляет поля,
добавляет новых, не создаёт дублей и никогда не меняет уже выданный токен.
"""
from dataclasses import dataclass, field

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Participant, Team
from app.services.normalize import normalize_email, normalize_phone, normalize_username
from app.services.sources import Row
from app.services.tokens import generate_invite_token

# Поле участника → возможные названия колонки (без учёта регистра). Первое найденное — используется.
# Покрывает и форму businesscup.zimran.io/en/apply, и таблицу «Business Cup 2.0 — кандидаты».
# SHEET_COLUMN_MAP задаёт свою колонку для поля — она проверяется первой.
DEFAULT_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "participation_type": ("Participation type",),
    "full_name": ("Full name", "Имя", "ФИО", "Name"),
    "email": ("Email", "Почта", "E-mail"),
    "phone": ("Phone", "Phone number", "Телефон"),
    "tg_username_form": ("Telegram username", "Telegram", "Телеграм"),
    "linkedin": ("LinkedIn or CV link", "LinkedIn / CV", "LinkedIn"),
    "university": ("University", "Университет"),
    "year_of_study": ("Year of study", "Курс"),
    "major": ("Major", "Специальность"),
    "zimran_interest": ("Interested in a career at Zimran",),
    "career_interest": ("Career interest", "Career interests"),
    "aiesec_project": ("What AIESEC project have you participated in before?", "Previous AIESEC project"),
    "team_role": ("Position in team", "Роль в команде"),
    "application_id": ("ID заявки", "Application ID"),
}
SELECTED_COLUMN_ALIASES = ("Selected", "Состояние", "Статус", "Status", "Отбор")
TEAM_COLUMN_ALIASES = ("Team", "Team name", "Команда")
TEXT_FIELDS = (
    "team_role",
    "application_id",
    "participation_type",
    "linkedin",
    "university",
    "year_of_study",
    "major",
    "zimran_interest",
    "career_interest",
    "aiesec_project",
)
SOLO_PREFIX = "solo:"
MAX_ERRORS_IN_REPORT = 20


class ImportAborted(Exception):
    """Источник не подходит целиком (нет обязательной колонки и т. п.) — ничего не записано."""


@dataclass
class ImportReport:
    rows: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    not_selected: int = 0
    deselected: int = 0
    teams_created: int = 0
    duplicates: int = 0  # повторные заявки того же email (взята последняя)
    deselect_blocked: int = 0  # столько человек пропало из списка разом — не сняли, это похоже на ошибку
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def imported(self) -> int:
        return self.created + self.updated + self.unchanged


@dataclass
class TeamsReport:
    rows: int = 0
    assigned: int = 0
    unchanged: int = 0
    teams_created: int = 0
    unknown_emails: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def solo_team_name(email: str) -> str:
    return f"{SOLO_PREFIX}{email}"


def team_display_name(team: Team | None, lang: str = "ru") -> str:
    if team is None or team.is_solo:
        return "Соло" if lang == "ru" else "Solo"
    return team.name


class _Columns:
    """Регистронезависимый поиск колонки по названию."""

    def __init__(self, rows: list[Row]) -> None:
        headers: dict[str, str] = {}
        for row in rows:
            for key in row:
                headers.setdefault(key.strip().lower(), key)
        self._headers = headers

    def headers(self) -> list[str]:
        return list(self._headers.values())

    def find(self, *names: str) -> str | None:
        for name in names:
            if name and (actual := self._headers.get(name.strip().lower())):
                return actual
        return None


def _norm_status(value: str) -> str:
    return " ".join(value.strip().lower().replace("ё", "е").split())


def is_selected_value(value: str, selected_values: frozenset[str]) -> bool:
    """«Прошёл отбор», «прошел», «Accepted ✅» — считаем отобранным, если значение равно одному из
    SHEET_SELECTED_VALUES или начинается с него («прошел отбор» ← «прошел»)."""
    status = _norm_status(value)
    if not status:
        return False
    for v in selected_values:
        v = _norm_status(v)
        if v and (status == v or status.startswith(v + " ")):
            return True
    return False


def _add_error(report_errors: list[str], message: str) -> None:
    if len(report_errors) < MAX_ERRORS_IN_REPORT:
        report_errors.append(message)
    elif len(report_errors) == MAX_ERRORS_IN_REPORT:
        report_errors.append("… остальные ошибки не показаны")


async def _teams_by_name(session: AsyncSession) -> dict[str, Team]:
    teams = (await session.scalars(select(Team))).all()
    return {t.name.lower(): t for t in teams}


async def _get_or_create_team(
    session: AsyncSession, teams: dict[str, Team], name: str, *, is_solo: bool = False
) -> tuple[Team, bool]:
    if team := teams.get(name.lower()):
        return team, False
    team = Team(name=name, is_solo=is_solo)
    session.add(team)
    await session.flush()
    teams[name.lower()] = team
    return team, True


async def _drop_empty_solo_teams(session: AsyncSession) -> None:
    has_members = select(Participant.id).where(Participant.team_id == Team.id).exists()
    await session.execute(delete(Team).where(Team.is_solo.is_(True), ~has_members))


async def import_applications(
    session: AsyncSession,
    rows: list[Row],
    *,
    column_map: dict[str, str] | None = None,
    selected_column: str = "Selected",
    selected_values: frozenset[str] = frozenset({"yes"}),
    team_column: str = "Team",
    assume_selected: bool = False,
    deselect_missing: bool = False,
) -> ImportReport:
    """Записывает отобранных в БД. Коммит делает вызывающий код."""
    overrides = column_map or {}
    cols = _Columns(rows)
    resolved = {
        f: cols.find(*([overrides[f]] if f in overrides else []), *aliases) for f, aliases in DEFAULT_COLUMN_ALIASES.items()
    }
    if not resolved["email"]:
        found = ", ".join(f"«{h}»" for h in cols.headers()[:12]) or "—"
        raise ImportAborted(f"Нет колонки с email («Email» или «Почта»). Нашёл колонки: {found}.")
    if not resolved["full_name"]:
        raise ImportAborted("В таблице нет колонки с именем («Full name» или «Имя»). Проверь SHEET_COLUMN_MAP.")
    sel_col = cols.find(selected_column, *SELECTED_COLUMN_ALIASES)
    if not sel_col and not assume_selected:
        raise ImportAborted(
            f"В таблице нет колонки отбора («{selected_column}» или «Состояние»). Добавь её или импортируй "
            "кнопкой «Импортировать всех из файла», если в файле только отобранные."
        )
    team_col = cols.find(team_column, *TEAM_COLUMN_ALIASES)

    report = ImportReport(rows=len(rows))
    existing = {p.email: p for p in (await session.scalars(select(Participant))).all()}
    selected_before = sum(p.is_selected for p in existing.values())
    taken_tokens = set((await session.scalars(select(Participant.invite_token))).all())
    teams = await _teams_by_name(session)
    # Человек мог подать заявку несколько раз — берём последнюю строку с его email.
    latest: dict[str, tuple[int, Row]] = {}
    for line_no, row in enumerate(rows, start=2):  # строка 1 — заголовок
        email = normalize_email(row.get(resolved["email"], ""))
        if not email:
            _add_error(report.errors, f"строка {line_no}: нет корректного email")
            continue
        if email in latest:
            report.duplicates += 1
        latest[email] = (line_no, row)

    for email, (line_no, row) in latest.items():
        participant = existing.get(email)
        selected = assume_selected or is_selected_value(row.get(sel_col, ""), selected_values)
        if not selected:
            report.not_selected += 1
            if participant and participant.is_selected:
                participant.is_selected = False
                report.deselected += 1
            continue

        full_name = row.get(resolved["full_name"], "").strip()
        if not full_name:
            _add_error(report.errors, f"строка {line_no} ({email}): пустое имя")
            continue

        phone_raw = row.get(resolved["phone"], "").strip() if resolved["phone"] else ""
        phone = normalize_phone(phone_raw)
        if phone_raw and not phone:
            report.warnings.append(f"строка {line_no} ({email}): не разобрал телефон «{phone_raw}»")

        values = {
            "full_name": full_name,
            "phone": phone,
            "phone_raw": phone_raw or None,
            "tg_username_form": normalize_username(row.get(resolved["tg_username_form"] or "", "")),
            **{f: (row.get(resolved[f], "").strip() or None) if resolved[f] else None for f in TEXT_FIELDS},
        }

        if participant is None:
            token = generate_invite_token()
            while token in taken_tokens:
                token = generate_invite_token()
            taken_tokens.add(token)
            participant = Participant(email=email, invite_token=token, is_selected=True, **values)
            session.add(participant)
            existing[email] = participant
            report.created += 1
        else:
            changed = [k for k, v in values.items() if getattr(participant, k) != v]
            for k in changed:
                setattr(participant, k, values[k])
            if not participant.is_selected:
                participant.is_selected = True
                changed.append("is_selected")
            if changed:
                report.updated += 1
            else:
                report.unchanged += 1

        team_name = row.get(team_col, "").strip() if team_col else ""
        if team_name:
            team, created = await _get_or_create_team(session, teams, team_name)
            report.teams_created += created
            if participant.team_id != team.id:
                participant.team_id = team.id
        elif participant.team_id is None:
            # Команды ещё нет (или задают её отдельным листом) — участник пока соло-команда.
            team, created = await _get_or_create_team(session, teams, solo_team_name(email), is_solo=True)
            participant.team_id = team.id

    if deselect_missing:
        # Источник — полный список прошедших: кого в нём больше нет, тому закрываем доступ.
        # Защита от случайно очищенной вкладки: если пропало слишком много сразу — ничего не снимаем.
        missing = [p for email, p in existing.items() if p.is_selected and email not in latest]
        limit = max(10, selected_before // 3)
        if len(missing) > limit:
            report.deselect_blocked = len(missing)
        else:
            for p in missing:
                p.is_selected = False
            report.deselected += len(missing)

    await session.flush()
    await _drop_empty_solo_teams(session)
    return report


async def assign_teams(session: AsyncSession, rows: list[Row]) -> TeamsReport:
    """Назначает команды по таблице «team_name, email» (отдельный лист или CSV)."""
    cols = _Columns(rows)
    team_col = cols.find("team_name", "team", "команда", "Team name")
    email_col = cols.find("email", "e-mail", "почта")
    if not team_col or not email_col:
        raise ImportAborted("Нужны колонки team_name и email.")

    report = TeamsReport(rows=len(rows))
    participants = {p.email: p for p in (await session.scalars(select(Participant))).all()}
    teams = await _teams_by_name(session)

    for line_no, row in enumerate(rows, start=2):
        email = normalize_email(row.get(email_col, ""))
        team_name = row.get(team_col, "").strip()
        if not email or not team_name:
            _add_error(report.errors, f"строка {line_no}: нужны и email, и команда")
            continue
        participant = participants.get(email)
        if participant is None:
            report.unknown_emails.append(email)
            continue
        team, created = await _get_or_create_team(session, teams, team_name)
        report.teams_created += created
        if participant.team_id == team.id:
            report.unchanged += 1
        else:
            participant.team_id = team.id
            report.assigned += 1

    await session.flush()
    await _drop_empty_solo_teams(session)
    return report


async def count_selected(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(Participant).where(Participant.is_selected)) or 0
