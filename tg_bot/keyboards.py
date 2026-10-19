from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup


MAIN_ACTIONS = {
    "▶️ Запустить парсер": "scan:start",
    "⏹ Остановить": "scan:stop",
    "🔎 Проверить новые": "scan:once",
    "⚙️ Фильтры": "filters",
    "📊 Статистика": "stats",
    "👤 Мой профиль": "profile",
    "📌 Сохранённые объявления": "saved",
    "🛡 Админ-панель": "admin",
}


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
        for row in rows
    ])


def main_menu(admin=False):
    labels = list(MAIN_ACTIONS)
    if not admin:
        labels.remove("🛡 Админ-панель")
    rows = [labels[index:index + 2] for index in range(0, len(labels), 2)]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=label) for label in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
    )


def filter_menu():
    return kb([("От даты", "filter:from"), ("До даты", "filter:to")],
              [("Категория", "filter:category"), ("Тип продавца", "filter:seller")],
              [("Количество", "filter:target")], [("◀️ Меню", "menu")])


def admin_menu():
    return kb([("Выдать", "admin:grant"), ("Продлить", "admin:extend")],
              [("Забрать", "admin:revoke"), ("Блокировать", "admin:block")],
              [("Разблокировать", "admin:unblock"), ("Найти", "admin:find")],
              [("Пользователи", "admin:users"), ("Статистика", "admin:stats")],
              [("Рассылка", "admin:broadcast"), ("Интервал", "admin:interval")],
              [("Вход OLX", "admin:login"), ("Вошёл в OLX", "admin:ready")],
              [("◀️ Меню", "menu")])
