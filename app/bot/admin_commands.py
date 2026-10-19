"""Подсказки команд в меню Telegram для админов — у каждого свой набор по роли."""
from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeChat

from app.db.models import ROLE_RANK, Role
from app.logs import get_logger

log = get_logger(__name__)

# (команда, описание, минимальная роль). Следующие этапы добавляют сюда свои команды.
ADMIN_COMMANDS: list[tuple[str, str, Role]] = [
    ("admin", "Админ-меню", Role.VOLUNTEER),
    ("find", "Найти участника: /find фамилия|телефон|email|команда", Role.VOLUNTEER),
    ("menu", "Меню участника", Role.VOLUNTEER),
    ("export", "Выгрузка bot_status.csv", Role.ADMIN),
    ("sync", "Записать статусы в Google Sheet", Role.ADMIN),
    ("content", "Редактировать расписание, локацию, FAQ, контакты", Role.ADMIN),
    ("broadcast", "Новая рассылка", Role.ADMIN),
    ("broadcasts", "Список рассылок, отмена", Role.ADMIN),
    ("poll", "Опрос явки: сводка, /poll start, /poll close", Role.ADMIN),
    ("calls", "Список на обзвон (CSV)", Role.ADMIN),
    ("stats", "Чекин: отмечено X/Y, команды", Role.ADMIN),
    ("checkin_open", "Открыть чекин сегодня", Role.ADMIN),
    ("checkin_close", "Закрыть чекин сегодня", Role.ADMIN),
    ("days", "Дни мероприятия, окна чекина", Role.ADMIN),
    ("screen", "Экран чекина и дашборд: ссылки, on/off", Role.ADMIN),
    ("suspicious", "Подозрительные события", Role.ADMIN),
    ("day", "Задать день: /day 14.11 08:00-13:00 День 1", Role.SUPERADMIN),
    ("import", "Импорт участников (CSV или Google Sheet)", Role.SUPERADMIN),
    ("import_teams", "Назначить команды (CSV team_name,email)", Role.SUPERADMIN),
    ("autoimport", "Автоимпорт из Google Sheet: on/off", Role.SUPERADMIN),
    ("addadmin", "Добавить: /addadmin <id|@username> <admin|volunteer>", Role.SUPERADMIN),
    ("removeadmin", "Убрать: /removeadmin <id|@username>", Role.SUPERADMIN),
    ("admins", "Список админов и волонтёров", Role.SUPERADMIN),
    ("audit", "Последние действия админов", Role.SUPERADMIN),
]


def commands_for(role: Role) -> list[BotCommand]:
    return [BotCommand(command=c, description=d) for c, d, r in ADMIN_COMMANDS if ROLE_RANK[role] >= ROLE_RANK[r]]


async def apply_admin_commands(bot: Bot, user_id: int, role: Role | None) -> None:
    """Ставит набор команд в личке с админом или убирает его (role=None). Ошибки не критичны:
    если человек ещё не открывал бота, Telegram ответит «chat not found»."""
    scope = BotCommandScopeChat(chat_id=user_id)
    try:
        if role is None:
            await bot.delete_my_commands(scope=scope)
        else:
            await bot.set_my_commands(commands_for(role), scope=scope)
    except Exception as exc:
        log.info("admin_commands_skipped", user_id=user_id, error=str(exc))
