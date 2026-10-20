"""Нормализация данных из формы: телефон в E.164, email и Telegram username."""
import re

_NON_DIGITS = re.compile(r"\D")
_USERNAME = re.compile(r"^[a-z][a-z0-9_]{3,31}$")  # username в Telegram начинается с буквы


def normalize_phone(raw: str | None) -> str | None:
    """Приводит номер к E.164. Казахстанские и российские номера (+7) — основной случай.

    8 701 234 56 78 → +77012345678
    +7 (701) 234-56-78 → +77012345678
    7012345678 (без кода страны) → +77012345678
    +998 90 123 45 67 → +998901234567
    Мусор и слишком короткие номера → None.
    """
    if not raw:
        return None
    text = raw.strip()
    has_plus = text.startswith("+") or text.startswith("00")
    digits = _NON_DIGITS.sub("", text)
    if text.startswith("00"):
        digits = digits[2:]

    if not has_plus:
        if len(digits) == 11 and digits[0] in "78":
            return "+7" + digits[1:]
        if len(digits) == 10 and digits[0] in "67":  # 70x/77x/747… мобильные KZ, 6xx — стационарные Алматы и др.
            return "+7" + digits
    if has_plus and 8 <= len(digits) <= 15:
        return "+" + digits
    if not has_plus and 11 <= len(digits) <= 15:
        # Номер с кодом страны, но без плюса (например, 998901234567).
        return "+" + digits
    return None


def normalize_email(raw: str | None) -> str | None:
    if not raw:
        return None
    email = raw.strip().lower()
    return email if "@" in email and "." in email.split("@")[-1] else None


def normalize_username(raw: str | None) -> str | None:
    """'@Ivan_Petrov' / 't.me/ivan_petrov' / 'https://t.me/ivan_petrov' → 'ivan_petrov'."""
    if not raw:
        return None
    name = raw.strip().lower()
    for prefix in ("https://", "http://", "www."):
        if name.startswith(prefix):
            name = name[len(prefix):]
    for prefix in ("t.me/", "telegram.me/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    name = name.lstrip("@").strip("/ ")
    return name if _USERNAME.match(name) else None
