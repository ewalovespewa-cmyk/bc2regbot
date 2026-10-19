# bc2regbot — бот участника Business Cup 2.0

Telegram-бот регистрации, коммуникации и чекина для Business Cup 2.0 (Zimran x AIESEC, 14–15 ноября,
Nazarbayev University). Пускает только отобранных участников, привязывает их Telegram по персональной
ссылке, рассылает новости и опрос явки, отмечает на входе по меняющемуся QR и сообщает боту игр
([bchbot](https://github.com/ewalovespewa-cmyk/bchbot)), кто активировал бота и кто пришёл.

> ⚠️ **Ровно один инстанс.** Webhook, фоновые воркеры (рассылки, напоминания, синхронизация) и лимиты
> рассчитаны на один процесс. Не увеличивай число реплик на Railway.

## Стек
Python 3.12, aiogram 3 (webhook), aiohttp, PostgreSQL (SQLAlchemy 2 async + Alembic), Redis
(FSM, дедупликация апдейтов, блокировки), structlog (JSON-логи).

## Структура
```
app/
  main.py          точка входа: aiohttp-сервер, webhook, /health
  config.py        все настройки из переменных окружения (описаны в .env.example)
  logs.py          JSON-логи
  db/              модели и подключение к Postgres
  bot/             диспетчер, middleware (дедупликация, сессия БД), хендлеры, обработчик ошибок
  web/             HTTP-эндпоинты (/health, позже экран чекина, дашборд, API)
  i18n/            тексты бота: ru.yaml, en.yaml
migrations/        миграции Alembic
tests/             unit/ и integration/ (настоящий Postgres)
.railway/          настройки деплоя (Railway Infrastructure as Code)
```

## Локальный запуск
Нужен Python 3.12. Docker не нужен: для тестов и разработки используется встроенный Postgres
из пакета `pgserver`.

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt -e ".[dev]"   # на macOS/Linux: .venv/bin/pip
cp .env.example .env    # вписать BOT_TOKEN dev-бота, WEBHOOK_SECRET, REDIS_URL
```

Поднять локальный Postgres (один раз, данные лежат в `.pgdata/`):
```bash
.venv/Scripts/python -c "import pgserver; s=pgserver.get_server('.pgdata', cleanup_mode=None); s.psql('create database bc_dev;'); print(s.get_uri())"
```

Применить миграции и запустить бота:
```bash
.venv/Scripts/alembic upgrade head
.venv/Scripts/python -m app.main
```
Без `WEBHOOK_BASE_URL` бот работает через polling. Используй для этого **отдельного dev-бота**:
polling снимает webhook, и боевой бот перестал бы получать сообщения.

Redis для локального запуска: Memurai или Redis в WSL, либо `REDIS_URL` dev-инстанса на Railway.

## Тесты
```bash
.venv/Scripts/python -m pytest -q
```
Интеграционные тесты сами поднимают встроенный Postgres, применяют миграции с нуля и проверяют
схему. Чтобы гонять их против другой базы, задай `TEST_DATABASE_URL`. Redis в тестах — fakeredis.

## Надёжность
- **Состояние только в Postgres и Redis.** Рестарт ничего не теряет.
- **Дедупликация апдейтов.** Повторная доставка того же `update_id` не обрабатывается дважды (Redis, 48 ч).
  Апдейт обрабатывается до ответа Telegram, поэтому при падении посреди обработки Telegram его повторит.
- **Ошибки.** Пользователь получает дружелюбное сообщение, в чат организаторов (`ADMIN_CHAT_ID`) уходит
  трейсбек. Одинаковые ошибки алертятся не чаще раза в `ERROR_ALERT_THROTTLE_SECONDS`.
- **`/health`** проверяет Postgres и Redis (таймаут 2 с) и отвечает 503, если что-то недоступно.
  Railway использует его как healthcheck.
- **Таймауты.** 30 с на запросы к Telegram, 10 с на подключение к Postgres и 30 с на запрос, 5 с на Redis.

## Railway
Бот живёт в Railway-проекте `bchbot` рядом с ботом игр: так они общаются по приватной сети.

Railway объявил `railway.json` (Config as Code) устаревшим, и к новым сервисам он не применяется.
Поэтому настройки деплоя описаны в [.railway/railway.ts](.railway/railway.ts) (Infrastructure as Code):
- сборка по `Dockerfile`;
- миграции командой pre-deploy (`alembic upgrade head`): если миграция упала, деплой не переключится;
- healthcheck `/health`;
- **1 реплика**;
- перезапуск при падении (политика Railway по умолчанию — `ON_FAILURE`).

Применить настройки через Railway CLI:
```bash
npm install --prefix .railway railway
railway login
railway link        # проект bchbot
railway config plan
railway config apply
```

Webhook бот ставит сам при каждом старте на `WEBHOOK_BASE_URL` + `WEBHOOK_PATH` с `WEBHOOK_SECRET`.
При остановке webhook не снимается: при деплое новый контейнер уже поставил свой.

Полная пошаговая инструкция (переменные, Google service account, экран чекина, дашборд) и чеклист
«за день до мероприятия» появятся на этапе 9.

## Обновление зависимостей
Диапазоны версий — в `pyproject.toml`, точные версии для образа — в `requirements.txt`.
Обновить: поставить зависимости в чистый venv (`pip install -e .`), прогнать тесты,
затем `pip freeze --exclude-editable > requirements.txt`.
