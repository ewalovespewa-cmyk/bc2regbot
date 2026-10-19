from app.services.checkin_codes import (
    WINDOW_SECONDS,
    current_code,
    deep_link,
    extract_code,
    is_valid,
    make_code,
    window_of,
)

SECRET = "s3cret"
T0 = 1_800_000_000  # начало окна: делится на 30
assert T0 % WINDOW_SECONDS == 0


def test_code_stable_within_window_and_changes_between():
    c1, left1 = current_code(SECRET, 1, T0)
    c2, left2 = current_code(SECRET, 1, T0 + 29.9)
    c3, _ = current_code(SECRET, 1, T0 + 30)
    assert c1 == c2 != c3
    assert (left1, left2) == (30, 1)
    assert len(c1) == 10 and c1.isalnum() and c1.isupper()


def test_current_and_previous_window_accepted():
    code = make_code(SECRET, 1, window_of(T0))
    assert is_valid(SECRET, 1, code, T0)  # то же окно
    assert is_valid(SECRET, 1, code, T0 + 59.9)  # следующее окно: ещё принимаем
    assert not is_valid(SECRET, 1, code, T0 + 60)  # через одно — устарел
    assert not is_valid(SECRET, 1, code, T0 - 0.1)  # из будущего окна — нет


def test_code_bound_to_day_and_secret():
    code = make_code(SECRET, 1, window_of(T0))
    assert not is_valid(SECRET, 2, code, T0)
    assert not is_valid("other", 1, code, T0)
    assert not is_valid("", 1, code, T0)
    assert is_valid(SECRET, 1, code.lower(), T0)  # регистр не важен


def test_extract_code():
    link = deep_link("bc_bot", "ABCDEFGHIJ")
    assert link == "https://t.me/bc_bot?start=ci_ABCDEFGHIJ"
    assert extract_code(link) == "ABCDEFGHIJ"
    assert extract_code("ci_XYZ") == "XYZ"
    assert extract_code("https://example.com") is None
    assert extract_code("") is None
