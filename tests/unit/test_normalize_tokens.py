import pytest

from app.services.normalize import normalize_email, normalize_phone, normalize_username
from app.services.tokens import TOKEN_LENGTH, generate_invite_token, invite_link, is_invite_token


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("87012345678", "+77012345678"),
        ("8 701 234 56 78", "+77012345678"),
        ("+7 (701) 234-56-78", "+77012345678"),
        ("77012345678", "+77012345678"),
        ("7012345678", "+77012345678"),
        ("+77012345678", "+77012345678"),
        ("8(727)2501234", "+77272501234"),
        ("0077012345678", "+77012345678"),
        ("+998 90 123 45 67", "+998901234567"),
        ("998901234567", "+998901234567"),
        ("+1 415 555 2671", "+14155552671"),
        ("", None),
        (None, None),
        ("нет", None),
        ("12345", None),
        ("+12", None),
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


def test_normalize_email():
    assert normalize_email("  Ivan.Petrov@Gmail.COM ") == "ivan.petrov@gmail.com"
    assert normalize_email("not-an-email") is None
    assert normalize_email("a@b") is None
    assert normalize_email(None) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("@Ivan_Petrov", "ivan_petrov"),
        ("ivan_petrov", "ivan_petrov"),
        ("https://t.me/Ivan_Petrov", "ivan_petrov"),
        ("t.me/ivan_petrov/", "ivan_petrov"),
        ("@abc", None),  # короче 4 символов — не username
        ("Иван", None),
        ("", None),
    ],
)
def test_normalize_username(raw, expected):
    assert normalize_username(raw) == expected


def test_tokens_unique_and_valid():
    tokens = {generate_invite_token() for _ in range(2000)}
    assert len(tokens) == 2000
    assert all(len(t) == TOKEN_LENGTH and is_invite_token(t) for t in tokens)
    assert not is_invite_token("ci_abcdefghi")
    assert invite_link("bc_bot", "AbC123") == "https://t.me/bc_bot?start=AbC123"
