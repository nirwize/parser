from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
        for row in rows
    ])


def main_menu(admin=False):
    rows = [
        [("▶️ Запустить парсер", "scan:start"), ("⏹ Остановить", "scan:stop")],
        [("🔎 Проверить новые", "scan:once"), ("⚙️ Фильтры", "filters")],
        [("📊 Статистика", "stats"), ("👤 Мой профиль", "profile")],
    ]
    if admin:
        rows.append([("🛡 Админ-панель", "admin")])
    return kb(*rows)


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
