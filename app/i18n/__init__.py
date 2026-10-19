"""Тексты бота. Основной язык русский, английский второй.

Ключи вида "errors.generic" ищутся в locales/<lang>.yaml; если ключа нет в языке
пользователя, берётся русский, если нет и там — возвращается сам ключ (видно в тестах).
"""
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

DEFAULT_LANG = "ru"
SUPPORTED_LANGS = ("ru", "en")
_DIR = Path(__file__).parent


@lru_cache
def _load(lang: str) -> dict[str, Any]:
    path = _DIR / f"{lang}.yaml"
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _lookup(tree: dict[str, Any], key: str) -> str | None:
    node: Any = tree
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, str) else None


def pick_lang(language_code: str | None) -> str:
    """Telegram language_code → один из поддерживаемых языков."""
    if language_code and language_code.split("-")[0].lower() == "en":
        return "en"
    return DEFAULT_LANG


def t(key: str, lang: str = DEFAULT_LANG, **kwargs: Any) -> str:
    lang = lang if lang in SUPPORTED_LANGS else DEFAULT_LANG
    text = _lookup(_load(lang), key) or _lookup(_load(DEFAULT_LANG), key) or key
    return text.format(**kwargs) if kwargs else text
