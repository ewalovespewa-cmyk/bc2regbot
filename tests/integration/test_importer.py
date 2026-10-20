"""Импорт заявок против настоящего Postgres: идемпотентность, команды, отбор."""
import pytest
from sqlalchemy import func, select

from app.db.models import Participant, Team
from app.services.exports import bot_status_table
from app.services.importer import ImportAborted, assign_teams, import_applications, solo_team_name
from app.services.sources import parse_csv

SELECTED = frozenset({"yes", "да"})

CSV = (
    "Participation type,Full name,Email,Phone,Telegram username,University,Selected,Team\n"
    "Team,Aru Bekova,Aru@Mail.com,8 701 111 22 33,@aru_b,NU,yes,Rockets\n"
    "Team,Dana Ospan,dana@mail.com,+7 702 222 33 44,dana_o,KBTU,да,Rockets\n"
    "Solo participant,Timur Ali,timur@mail.com,87053334455,,SDU,yes,\n"
    "Solo participant,Not Picked,nope@mail.com,87054445566,,SDU,no,\n"
)


async def run(session, rows, **kw):
    report = await import_applications(session, rows, selected_values=SELECTED, **kw)
    await session.commit()
    return report


async def participants(session) -> dict[str, Participant]:
    rows = (await session.scalars(select(Participant))).all()
    return {p.email: p for p in rows}


async def test_import_creates_only_selected(session):
    report = await run(session, parse_csv(CSV.encode()))
    assert (report.created, report.not_selected, report.errors) == (3, 1, [])

    people = await participants(session)
    assert set(people) == {"aru@mail.com", "dana@mail.com", "timur@mail.com"}
    aru = people["aru@mail.com"]
    assert aru.phone == "+77011112233"
    assert aru.tg_username_form == "aru_b"
    assert aru.university == "NU"


async def test_reimport_is_idempotent_and_keeps_tokens(session):
    rows = parse_csv(CSV.encode())
    await run(session, rows)
    tokens_before = {e: p.invite_token for e, p in (await participants(session)).items()}

    report = await run(session, rows)
    assert (report.created, report.updated, report.unchanged) == (0, 0, 3)
    assert await session.scalar(select(func.count()).select_from(Participant)) == 3
    assert await session.scalar(select(func.count()).select_from(Team)) == 2  # Rockets + соло Тимура

    session.expire_all()
    tokens_after = {e: p.invite_token for e, p in (await participants(session)).items()}
    assert tokens_after == tokens_before


async def test_reimport_updates_fields_and_adds_new(session):
    await run(session, parse_csv(CSV.encode()))
    changed = CSV.replace("SDU,yes,", "AITU,yes,") + "Team,New Person,new@mail.com,87001112233,,NU,yes,Rockets\n"
    report = await run(session, parse_csv(changed.encode()))
    assert (report.created, report.updated, report.unchanged) == (1, 1, 2)
    session.expire_all()
    people = await participants(session)
    assert people["timur@mail.com"].university == "AITU"
    assert people["new@mail.com"].team.name == "Rockets"


async def test_deselect_and_reselect(session):
    await run(session, parse_csv(CSV.encode()))
    report = await run(session, parse_csv(CSV.replace("SDU,yes,", "SDU,no,").encode()))
    assert report.deselected == 1
    session.expire_all()
    assert (await participants(session))["timur@mail.com"].is_selected is False

    await run(session, parse_csv(CSV.encode()))
    session.expire_all()
    assert (await participants(session))["timur@mail.com"].is_selected is True


async def test_teams_and_solo(session):
    await run(session, parse_csv(CSV.encode()))
    people = await participants(session)
    assert people["aru@mail.com"].team_id == people["dana@mail.com"].team_id
    timur_team = await session.get(Team, people["timur@mail.com"].team_id)
    assert timur_team.is_solo and timur_team.name == solo_team_name("timur@mail.com")


async def test_separate_teams_sheet_moves_solo_into_team(session):
    await run(session, parse_csv(CSV.encode()))
    teams_csv = "team_name,email\nComets,timur@mail.com\nComets,unknown@mail.com\n"
    report = await assign_teams(session, parse_csv(teams_csv.encode()))
    await session.commit()
    assert (report.assigned, report.unknown_emails) == (1, ["unknown@mail.com"])

    # Пустая соло-команда удалена, а повторный импорт без колонки Team команду не сбрасывает.
    assert await session.scalar(select(func.count()).select_from(Team).where(Team.is_solo)) == 0
    await run(session, parse_csv(CSV.encode()))
    session.expire_all()
    assert (await participants(session))["timur@mail.com"].team.name == "Comets"


async def test_custom_column_map_and_case_insensitive_headers(session):
    csv_text = "ФИО;Почта;Отобран\nАружан Б;aru@mail.com;Да\n"
    report = await run(
        session,
        parse_csv(csv_text.encode("utf-8-sig")),
        column_map={"full_name": "фио", "email": "ПОЧТА"},
        selected_column="отобран",
    )
    assert report.created == 1


async def test_missing_selected_column_aborts_unless_all(session):
    rows = parse_csv(b"Full name,Email\nA B,a@b.kz\n")
    with pytest.raises(ImportAborted):
        await import_applications(session, rows)
    report = await run(session, rows, assume_selected=True)
    assert report.created == 1


async def test_bad_rows_reported_not_fatal(session):
    csv_text = "Full name,Email,Selected,Phone\n,x@y.kz,yes,\nNo Email,,yes,\nOk Name,ok@y.kz,yes,abc\n"
    report = await run(session, parse_csv(csv_text.encode()))
    assert report.created == 1
    assert len(report.errors) == 2
    assert any("телефон" in w for w in report.warnings)


async def test_bot_status_table(session):
    await run(session, parse_csv(CSV.encode()))
    header, rows = await bot_status_table(session, "bc_bot", "Asia/Almaty")
    col = {h: i for i, h in enumerate(header)}
    by_email = {r[col["Почта"]]: r for r in rows}
    assert set(by_email) == {"aru@mail.com", "dana@mail.com", "timur@mail.com"}
    assert by_email["timur@mail.com"][col["Команда"]] == "Соло"
    assert by_email["aru@mail.com"][col["Ссылка-приглашение"]].startswith("https://t.me/bc_bot?start=")
    assert by_email["aru@mail.com"][col["Открыл бота"]] == "нет"
