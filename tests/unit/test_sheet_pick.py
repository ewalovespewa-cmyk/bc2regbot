"""Какой лист бот считает источником заявок."""
from types import SimpleNamespace

from app.config import Settings
from app.services.sources import SheetsClient


def _client(titles: list[str]) -> SheetsClient:
    client = SheetsClient(Settings(_env_file=None, sheet_id="x", google_service_account_json="{}"))
    sheets = [SimpleNamespace(title=t) for t in titles]
    client._spreadsheet = SimpleNamespace(worksheets=lambda: sheets, worksheet=lambda n: next(s for s in sheets if s.title == n))
    return client


def test_prefers_candidates_tab_anywhere():
    assert _client(["Sheet1", "Прошли", "Кандидаты"])._worksheet("").title == "Кандидаты"


def test_skips_bot_own_tabs():
    assert _client(["Прошли", "Чекин День 1", "Заявки 2026"])._worksheet("").title == "Заявки 2026"


def test_explicit_name_wins():
    assert _client(["Кандидаты", "Мой лист"])._worksheet("Мой лист").title == "Мой лист"
