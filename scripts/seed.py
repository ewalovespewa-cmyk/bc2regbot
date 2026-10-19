"""Сидинг: 300 фейковых участников в 100 командах (по 1–3 человека).

    python -m scripts.seed                    # в базу из DATABASE_URL
    python -m scripts.seed --linked           # сразу «привязать» фейковые Telegram ID (для нагрузки)
    python -m scripts.seed --csv seed.csv     # не трогать БД, а записать CSV для /import

Повторный запуск не создаёт дублей (импорт идемпотентен по email).
"""
import argparse
import asyncio
import csv
import random
from pathlib import Path

FIRST = ["Аружан", "Нурсултан", "Дана", "Алихан", "Айгерим", "Тимур", "Мадина", "Ерлан", "Камила", "Санжар",
         "Асель", "Даурен", "Алия", "Бекзат", "Жанель", "Арман", "Томирис", "Ильяс", "Сабина", "Олжас"]
LAST = ["Беков", "Оспанов", "Ахметов", "Сапаров", "Касымов", "Нурланов", "Жумабаев", "Ержанов", "Мусин", "Аскаров",
        "Тулегенов", "Серикбаев", "Абенов", "Калиев", "Омаров"]
UNIS = ["Nazarbayev University", "KBTU", "SDU", "AITU", "ENU", "KIMEP", "Satbayev University"]
TG_BASE = 7_000_000_000  # фейковые Telegram ID, не пересекаются с реальными на практике


def build_rows(participants: int = 300, teams: int = 100, seed: int = 2026) -> list[dict[str, str]]:
    rnd = random.Random(seed)
    sizes = [1] * teams
    for _ in range(participants - teams):  # раскидываем остальных по командам, максимум 3
        candidates = [i for i, s in enumerate(sizes) if s < 3]
        sizes[rnd.choice(candidates)] += 1
    rows = []
    n = 0
    for t, size in enumerate(sizes, start=1):
        team = f"Team {t:03d}" if size > 1 else ""
        for _ in range(size):
            n += 1
            first, last = rnd.choice(FIRST), rnd.choice(LAST)
            rows.append(
                {
                    "Participation type": "Team" if team else "Solo participant",
                    "Full name": f"{first} {last}",
                    "Email": f"seed{n:03d}@example.com",
                    "Phone": f"8 70{n:08d}",  # уникальные казахстанские номера: 8 70XXXXXXXX
                    "Telegram username": f"@seed_user_{n:03d}",
                    "University": rnd.choice(UNIS),
                    "Year of study": str(rnd.randint(1, 4)),
                    "Major": rnd.choice(["CS", "Finance", "Marketing", "Economics", "Design"]),
                    "Interested in a career at Zimran": rnd.choice(["Yes", "No"]),
                    "Selected": "yes",
                    "Team": team,
                }
            )
    return rows


async def seed_db(rows: list[dict[str, str]], linked: bool) -> None:
    from sqlalchemy import select, update

    from app.config import get_settings
    from app.db.models import Participant
    from app.db.session import create_engine, create_sessionmaker
    from app.services.sheet_sync import import_rows

    settings = get_settings()
    engine = create_engine(settings.database_url)
    async with create_sessionmaker(engine)() as session:
        report = await import_rows(session, settings, rows)
        if linked:
            ids = (await session.scalars(select(Participant.id).where(Participant.email.like("seed%@example.com")))).all()
            for pid in ids:
                await session.execute(
                    update(Participant).where(Participant.id == pid, Participant.telegram_user_id.is_(None))
                    .values(telegram_user_id=TG_BASE + pid, telegram_username=f"seed_user_{pid:03d}")
                )
        await session.commit()
    await engine.dispose()
    print(f"Импорт: новых {report.created}, обновлено {report.updated}, без изменений {report.unchanged}"
          f"{', Telegram привязан' if linked else ''}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--participants", type=int, default=300)
    parser.add_argument("--teams", type=int, default=100)
    parser.add_argument("--linked", action="store_true", help="проставить фейковые Telegram ID")
    parser.add_argument("--csv", type=Path, help="записать CSV вместо БД")
    args = parser.parse_args()
    rows = build_rows(args.participants, args.teams)
    if args.csv:
        with args.csv.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(f"Записал {len(rows)} строк в {args.csv}")
        return
    asyncio.run(seed_db(rows, args.linked))


if __name__ == "__main__":
    main()
