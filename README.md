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
  services/        бизнес-логика: импорт, нормализация, токены, выгрузки, роли
  workers/         фоновый планировщик (лидер-лок в Redis) и периодические задачи
  bot/             диспетчер, middleware (дедупликация, сессия БД), хендлеры, обработчик ошибок
  web/             HTTP-эндпоинты (/health, позже экран чекина, дашборд, API)
  i18n/            тексты бота: ru.yaml, en.yaml
migrations/        миграции Alembic
tests/             unit/ и integration/ (настоящий Postgres)
samples/           пример CSV заявок для ручной проверки
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

## Импорт участников и персональные ссылки
Источник — таблица заявок с сайта (Google Sheet) или её CSV-выгрузка. Импортируются только
строки, где в колонке отбора (`SHEET_SELECTED_COLUMN`, по умолчанию `Selected`) стоит одно из
значений `SHEET_SELECTED_VALUES` (`yes`, `да`, `1`, `true`, `+`, `✅`).

| Команда | Кто | Что делает |
|---|---|---|
| `/import` + CSV-файл (в подписи или ответом на файл) | superadmin | импорт из файла |
| `/import all` + CSV | superadmin | импорт файла, где все строки уже отобраны (колонки отбора нет) |
| `/import` без файла | superadmin | импорт из Google Sheet (если подключён) |
| `/import_teams` + CSV `team_name,email` | superadmin | назначить команды отдельным файлом или листом |
| `/export` | admin | файл `bot_status.csv`: ссылки, статус бота, опрос, чекины |
| `/sync` | admin | записать то же на лист `bot_status` в Google Sheet |
| `/autoimport on\|off` | superadmin | автоимпорт из Sheet каждые `AUTO_IMPORT_MINUTES` минут |

Как работает импорт:
- **Идемпотентно.** Ключ — email в нижнем регистре. Повторный импорт обновляет поля и добавляет
  новых, дублей не создаёт, **уже выданные токены не меняет** (ссылки из отправленных писем остаются рабочими).
- **Снятые с отбора.** Если в повторном импорте у человека стоит «не отобран», запись остаётся,
  но бот перестаёт его пускать.
- **Нормализация.** Телефон приводится к E.164 (`8 701 …` → `+7701…`), Telegram username — к виду без `@`
  в нижнем регистре.
- **Команды.** Берутся из колонки `Team` или отдельного файла/листа `team_name,email`. Без команды
  участник считается соло. Пустая колонка `Team` при повторном импорте не сбрасывает команду,
  назначенную отдельным файлом.
- **Колонки.** Названия ищутся без учёта регистра. Если в таблице они другие, задай
  `SHEET_COLUMN_MAP` (см. `.env.example`).
- **Ошибки.** Плохие строки (нет email, пустое имя) пропускаются и перечисляются в отчёте. Если нет
  обязательной колонки, импорт отменяется целиком и ничего не записывается.

Персональная ссылка участника — `https://t.me/<бот>?start=<токен>` (12 символов, буквы и цифры).
Ссылки берутся из колонки `invite_link` в `/export` или на листе `bot_status`, оттуда их подставляют
в email-рассылку. Письма отправляет команда, не бот.

**Google Sheet** подключается переменными `SHEET_ID` и `GOOGLE_SERVICE_ACCOUNT_JSON`. Лист `bot_status`
бот перезаписывает целиком одним пакетом каждые `SHEET_SYNC_MINUTES` минут и по `/sync`. Все обращения
к Sheets идут в фоне с таймаутом и ретраями. Если Sheets недоступен, бот работает дальше, а ошибка
уходит в чат организаторов.

Пример файла для проверки: [samples/applications_example.csv](samples/applications_example.csv).

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
