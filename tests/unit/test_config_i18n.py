from app.config import Settings, normalize_db_url
from app.i18n import pick_lang, t


def test_db_url_normalized_for_asyncpg():
    assert normalize_db_url("postgres://u:p@h:5432/db") == "postgresql+asyncpg://u:p@h:5432/db"
    assert normalize_db_url("postgresql://u:p@h/db") == "postgresql+asyncpg://u:p@h/db"
    assert normalize_db_url("postgresql+asyncpg://u@h/db") == "postgresql+asyncpg://u@h/db"


def test_superadmin_ids_from_comma_list(monkeypatch):
    monkeypatch.setenv("SUPERADMIN_IDS", " 111, 222,,333 ")
    s = Settings(_env_file=None)
    assert s.superadmin_ids == frozenset({111, 222, 333})


def test_admin_chat_id_tolerates_copy_paste(monkeypatch):
    for raw, expected in [("-1001234567890", -1001234567890), (" #-100123 ", -100123), ("", None), ("  ", None)]:
        monkeypatch.setenv("ADMIN_CHAT_ID", raw)
        assert Settings(_env_file=None).admin_chat_id == expected


def test_webhook_url(monkeypatch):
    monkeypatch.setenv("WEBHOOK_BASE_URL", "https://bot.example.com/")
    s = Settings(_env_file=None)
    assert s.webhook_url == "https://bot.example.com/tg/webhook"
    monkeypatch.setenv("WEBHOOK_BASE_URL", "")
    assert Settings(_env_file=None).webhook_url == ""


def test_pick_lang():
    assert pick_lang("en") == "en"
    assert pick_lang("en-US") == "en"
    assert pick_lang("kk") == "ru"
    assert pick_lang(None) == "ru"


def test_translation_fallbacks():
    assert t("errors.generic", "en").startswith("Something went wrong")
    assert t("errors.generic", "kk") == t("errors.generic", "ru")
    assert t("no.such.key", "ru") == "no.such.key"
