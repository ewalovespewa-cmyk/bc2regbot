"""Коды меняющегося QR на экране чекина (по принципу TOTP).

Код не хранится в БД: он вычисляется из секрета, дня и номера 30-секундного окна.
Принимаются текущее и предыдущее окно, то есть код живёт 30–60 секунд — сканирование
на границе окна не ломается.
"""
import base64
import hashlib
import hmac
import time

WINDOW_SECONDS = 30
CODE_LENGTH = 10
PREFIX = "ci_"


def window_of(ts: float) -> int:
    return int(ts // WINDOW_SECONDS)


def make_code(secret: str, day_id: int, window: int) -> str:
    digest = hmac.new(secret.encode(), f"{day_id}:{window}".encode(), hashlib.sha256).digest()
    return base64.b32encode(digest).decode()[:CODE_LENGTH]


def current_code(secret: str, day_id: int, now: float | None = None) -> tuple[str, int]:
    """(код, секунд до смены)."""
    now = time.time() if now is None else now
    window = window_of(now)
    seconds_left = WINDOW_SECONDS - int(now - window * WINDOW_SECONDS)
    return make_code(secret, day_id, window), seconds_left


def is_valid(secret: str, day_id: int, code: str, now: float | None = None) -> bool:
    if not secret or not code:
        return False
    now = time.time() if now is None else now
    window = window_of(now)
    code = code.strip().upper()
    return any(hmac.compare_digest(make_code(secret, day_id, w), code) for w in (window, window - 1))


def deep_link(bot_username: str, code: str) -> str:
    return f"https://t.me/{bot_username}?start={PREFIX}{code}"


def extract_code(text: str) -> str | None:
    """Достаёт код из deep link, payload «ci_XXXX» или текста QR, отсканированного в Mini App."""
    if not text:
        return None
    text = text.strip()
    marker = f"start={PREFIX}"
    if marker in text:
        return text.split(marker, 1)[1].split("&")[0].strip() or None
    if text.startswith(PREFIX):
        return text[len(PREFIX):] or None
    return None
