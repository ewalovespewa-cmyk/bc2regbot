"""Персональные инвайт-токены для ссылок t.me/<bot>?start=<token>."""
import secrets
import string

# Только буквы и цифры: такой токен не путается с префиксами служебных deep link (ci_…)
# и переживает копирование из письма. 12 символов base62 ≈ 71 бит.
ALPHABET = string.ascii_letters + string.digits
TOKEN_LENGTH = 12


def generate_invite_token() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(TOKEN_LENGTH))


def is_invite_token(value: str) -> bool:
    return len(value) == TOKEN_LENGTH and all(c in ALPHABET for c in value)


def invite_link(bot_username: str, token: str) -> str:
    return f"https://t.me/{bot_username}?start={token}"
