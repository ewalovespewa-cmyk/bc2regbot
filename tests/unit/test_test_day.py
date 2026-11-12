from datetime import date

from app.db.models import EventDay
from app.services.checkin import day_number, is_test_day


def _day(id_, d, label):
    day = EventDay(date=d, label=label)
    day.id = id_
    return day


def test_test_day_hidden_from_numbering():
    test = _day(1, date(2026, 11, 12), "Тест")
    d1 = _day(2, date(2026, 11, 14), "День 1")
    d2 = _day(3, date(2026, 11, 15), "День 2")
    days = [test, d1, d2]
    assert is_test_day(test) and is_test_day(_day(4, date(2026, 11, 12), "🧪 бинго")) and is_test_day(_day(5, date(2026, 11, 12), " test"))
    assert not is_test_day(d1)
    assert day_number(d1, days) == 1 and day_number(d2, days) == 2
