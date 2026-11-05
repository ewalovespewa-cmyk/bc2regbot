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
    assert _client(["Статус бота", "Чекин День 1", "Заявки 2026"])._worksheet("").title == "Заявки 2026"


def test_skips_tabs_marked_bot():
    assert _client(["Прошли отбор (BOT)", "Заявки 2026"])._worksheet("").title == "Заявки 2026"


def test_explicit_name_wins():
    assert _client(["Кандидаты", "Мой лист"])._worksheet("Мой лист").title == "Мой лист"


def test_source_tab_by_name_and_aliases():
    import pytest

    from app.services.sources import SheetMissing

    assert _client(["Кандидаты", "Прошли отбор"])._worksheet("Прошли отбор").title == "Прошли отбор"
    assert _client(["Кандидаты", "прошедшие"])._worksheet("Прошли отбор").title == "прошедшие"
    with pytest.raises(SheetMissing):
        _client(["Кандидаты", "Статус бота"])._worksheet("Прошли отбор")


def test_exact_name_beats_alias():
    tabs = ["Прошли отбор", "Прошли отбор (BOT)"]
    assert _client(tabs)._worksheet("Прошли отбор (BOT)").title == "Прошли отбор (BOT)"
    assert _client(["Прошли отбор"])._worksheet("Прошли отбор (BOT)").title == "Прошли отбор"


class _Tab:
    def __init__(self, title, header=()):
        self.title, self.header, self.written = title, list(header), None

    def row_values(self, n):
        return self.header

    def update(self, values, cell, value_input_option=None):
        self.written = values


def _creating_client(tabs):
    client = _client([])
    created = []

    def add_worksheet(name, rows, cols):
        created.append(_Tab(name))
        return created[-1]

    client._spreadsheet = SimpleNamespace(worksheets=lambda: tabs, add_worksheet=add_worksheet)
    return client, created


async def test_source_tab_copies_candidates_header():
    client, created = _creating_client([_Tab("Кандидаты", ["Имя", "Почта"])])
    await client.create_source_tab("Прошли отбор (BOT)")
    assert created[0].title == "Прошли отбор (BOT)" and created[0].written == [["Имя", "Почта"]]
    assert client.last_read_title == "Прошли отбор (BOT)"


async def test_source_tab_without_candidates_gets_default_header():
    client, created = _creating_client([_Tab("Лист1")])
    await client.create_source_tab("Прошли отбор (BOT)")
    assert "Почта" in created[0].written[0] and "Telegram" in created[0].written[0]


async def test_missing_source_tab_is_created_and_nobody_is_touched(monkeypatch):
    import pytest

    from app.services import sheet_sync
    from app.services.importer import ImportAborted
    from app.services.sources import SheetMissing

    made = []

    class FakeClient:
        last_read_title = "Прошли отбор (BOT)"

        def __init__(self, settings):
            pass

        async def read_rows(self, worksheet):
            raise SheetMissing("нет вкладки")

        async def create_source_tab(self, name):
            made.append(name)

    monkeypatch.setattr(sheet_sync, "SheetsClient", FakeClient)
    settings = Settings(_env_file=None, sheet_id="x", google_service_account_json="{}")
    with pytest.raises(ImportAborted, match="пустой"):
        await sheet_sync.import_from_sheet(None, settings)  # до БД не дошло: снимать с отбора некого
    assert made == ["Прошли отбор (BOT)"]
