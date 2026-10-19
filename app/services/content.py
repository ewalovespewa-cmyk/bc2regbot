"""Редактируемые тексты: расписание, локация, FAQ, контакты.

Пока админ ничего не задал, показывается текст по умолчанию из locales (content.<key>).
"""
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import ContentPage
from app.i18n import t

PAGES = ("schedule", "location", "faq", "contacts")
# Nazarbayev University, Кабанбай батыра 53, Астана.
DEFAULT_LOCATION = (51.0905, 71.3985)


@dataclass
class Page:
    text_html: str
    latitude: float | None = None
    longitude: float | None = None


async def get_page(session: AsyncSession, settings: Settings, key: str, lang: str) -> Page:
    row = await session.get(ContentPage, (key, lang))
    if row is None and lang != "ru":
        row = await session.get(ContentPage, (key, "ru"))
    if row is not None:
        return Page(row.text_html, row.latitude, row.longitude)

    text = t(f"content.{key}", lang, contact=settings.organizer_contact)
    if key == "location":
        return Page(text, *DEFAULT_LOCATION)
    return Page(text)
