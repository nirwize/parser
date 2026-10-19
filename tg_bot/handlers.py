from __future__ import annotations

import html
from datetime import datetime
from types import SimpleNamespace

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .database import active, now
from .filters import category_names, parse_duration, validate_date, validate_filters
from .keyboards import MAIN_ACTIONS, admin_menu, filter_menu, kb, main_menu
from .navigation import Navigation


SAVED_PAGE_SIZE = 8


class Input(StatesGroup):
    filter_value = State()
    admin_user = State()
    duration = State()
    broadcast = State()
    interval = State()


def profile_text(user, admin_id):
    end = user["expires_at"]
    if user["blocked"]:
        status = "заблокирован"
    elif active(user, admin_id):
        status = "активен"
    else:
        status = "нет доступа"
    remaining = "без ограничения" if user["lifetime"] else "—"
    if end and active(user, admin_id):
        seconds = max(0, int((datetime.fromisoformat(end) - now()).total_seconds()))
        remaining = f"{seconds // 86400} дн. {(seconds % 86400) // 3600} ч."
    return (f"👤 @{html.escape(user['username'] or 'без username')} · ID {user['tg_id']}\n"
            f"Статус: {status}\n"
            f"Активирован: {user['activated_at'] or '—'}\n"
            f"Истекает: {end or ('никогда' if user['lifetime'] else '—')}\n"
            f"Осталось: {remaining}\n"
            f"Найдено: {user['found_count']}")


def filters_text(user, config):
    category = "Все категории" if user["category"] == "all" else user["category"]
    seller = "Только частные" if user["seller_mode"] == "private" else "Все продавцы"
    return (f"⚙️ Фильтры\nДата регистрации: {user['registered_from']} — {user['registered_to']}\n"
            f"Категория: {html.escape(category)}\nПродавцы: {seller}\n"
            f"Найти за проверку: {user['target']}\n"
            f"Категории берутся из config.json ({len(category_names(config))} включено).")


def setup(db, settings, service):
    router = Router()
    nav = Navigation(db, service.bot)
    router.message.filter(F.chat.type == "private")
    router.callback_query.filter(F.message.chat.type == "private")

    async def authorized(query: CallbackQuery, admin=False):
        uid = query.from_user.id
        if admin and uid != settings.admin_id:
            await query.answer("Только для владельца.", show_alert=True)
            return False
        if not active(await db.user(uid), settings.admin_id):
            await query.answer("Нет активного доступа. Напишите владельцу.", show_alert=True)
            return False
        return True

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext):
        await state.clear()
        user = await db.register(message.from_user.id, message.from_user.username)
        await nav.ensure_keyboard(message.from_user.id, main_menu(message.from_user.id == settings.admin_id))
        intro = "Бот OLX готов. "
        if not active(user, settings.admin_id):
            intro += "Для доступа попросите владельца выдать подписку. Ваш ID: " + str(user["tg_id"])
        else:
            intro += "Выберите действие."
        await nav.show(message.from_user.id, intro)

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext):
        await state.clear()
        await nav.ensure_keyboard(message.from_user.id, main_menu(message.from_user.id == settings.admin_id))
        await nav.show(message.from_user.id, "Ввод отменён.")

    @router.message(F.text.in_(set(MAIN_ACTIONS)))
    async def main_button(message: Message, state: FSMContext):
        action = MAIN_ACTIONS[message.text]
        uid = message.from_user.id
        try:
            await service.bot.delete_message(chat_id=uid, message_id=message.message_id)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass
        await state.clear()
        await nav.ensure_keyboard(uid, main_menu(uid == settings.admin_id))
        if action == "admin" and uid != settings.admin_id:
            await nav.show(uid, "Только для владельца.")
            return
        if action != "profile" and not active(await db.user(uid), settings.admin_id):
            await nav.show(uid, "Нет активного доступа. Напишите владельцу.")
            return

        async def acknowledge(*args, **kwargs):
            return None

        query = SimpleNamespace(from_user=message.from_user, message=message,
                                data=action, answer=acknowledge)
        await {
            "profile": profile, "stats": stats, "filters": filters,
            "saved": saved,
            "admin": admin, "scan:start": scan, "scan:stop": scan,
            "scan:once": scan,
        }[action](query)

    @router.callback_query(F.data == "menu")
    async def menu(query: CallbackQuery, state: FSMContext):
        await state.clear()
        await query.answer()
        await nav.ensure_keyboard(query.from_user.id, main_menu(query.from_user.id == settings.admin_id))
        await nav.show(query.from_user.id, "Главное меню")

    @router.callback_query(F.data == "profile")
    async def profile(query: CallbackQuery):
        await query.answer()
        user = await db.user(query.from_user.id)
        if user:
            await nav.show(query.from_user.id, profile_text(user, settings.admin_id), parse_mode="HTML")
        else:
            await nav.show(query.from_user.id, "Сначала нажмите /start.")

    @router.callback_query(F.data == "stats")
    async def stats(query: CallbackQuery):
        if not await authorized(query):
            return
        await query.answer()
        rows = await db.one("""SELECT count(*) runs, coalesce(sum(checked),0) checked,
          coalesce(sum(accepted),0) accepted FROM scan_runs WHERE tg_id=?""", (query.from_user.id,))
        user = await db.user(query.from_user.id)
        await nav.show(query.from_user.id, f"Проверок: {rows['runs']}\nПросмотрено: {rows['checked']}\n"
                                   f"Подошло: {rows['accepted']}\nОтправлено уникальных: {user['found_count']}")

    async def show_saved(uid, page=0):
        total = await db.saved_count(uid)
        if total == 0:
            await nav.show(uid, "📌 Сохранённых объявлений пока нет. Они появятся после успешной проверки.")
            return
        max_page = (total - 1) // SAVED_PAGE_SIZE
        page = min(page, max_page)
        offset = page * SAVED_PAGE_SIZE
        items = await db.saved_page(uid, SAVED_PAGE_SIZE, offset)
        lines = [f"📌 Сохранённые объявления: {total} · страница {page + 1}/{max_page + 1}"]
        for number, item in enumerate(items, start=offset + 1):
            title = html.escape(item["title"][:120])
            category = html.escape(item["category"][:80])
            price = html.escape(item["price_eur"][:40])
            url = html.escape(item["url"], quote=True)
            lines.append(f'{number}. <b>{title}</b> · {price} € · {category}\n<a href="{url}">Открыть объявление</a>')
        buttons = []
        if page > 0:
            buttons.append(("◀️ Назад", f"saved:{page - 1}"))
        if page < max_page:
            buttons.append(("Далее ▶️", f"saved:{page + 1}"))
        await nav.show(uid, "\n\n".join(lines), parse_mode="HTML",
                       reply_markup=kb(buttons) if buttons else None)

    @router.callback_query(F.data == "saved")
    async def saved(query: CallbackQuery):
        if not await authorized(query):
            return
        await query.answer()
        await show_saved(query.from_user.id)

    @router.callback_query(F.data.startswith("saved:"))
    async def saved_page(query: CallbackQuery):
        if not await authorized(query):
            return
        value = query.data.split(":", 1)[1]
        if not value.isdecimal() or int(value) > 1000000:
            await query.answer("Неверная страница.", show_alert=True)
            return
        await query.answer()
        await show_saved(query.from_user.id, int(value))

    @router.callback_query(F.data.in_({"scan:start", "scan:stop", "scan:once"}))
    async def scan(query: CallbackQuery):
        if not await authorized(query):
            return
        await query.answer()
        uid = query.from_user.id
        if query.data == "scan:stop":
            await service.stop(uid)
            await nav.show(query.from_user.id, "Автопроверка остановлена; текущая проверка прерывается.")
        elif query.data == "scan:start":
            user = await db.user(uid)
            was_blocked = await db.option("scan_blocked") == "1"
            if user["running"] and service.profile_available() and not was_blocked:
                await nav.show(query.from_user.id, "Автопроверка уже включена. Повторный запуск не нужен.")
                return
            if was_blocked and service.busy():
                await nav.show(query.from_user.id, "Парсер занят. Повторите запуск после окончания текущей проверки.")
                return
            await db.execute("UPDATE users SET running=1 WHERE tg_id=?", (uid,))
            if not service.launch(uid):
                await nav.show(uid, "Автопроверка включена. Сейчас парсер занят; первая проверка начнётся, когда он освободится.")
            else:
                await nav.show(uid, "Автопроверка включена. Ход проверки появится отдельным сообщением.")
        else:
            if not service.launch(uid):
                await nav.show(uid, "Парсер занят другой проверкой. Попробуйте позже.")
            else:
                await nav.show(uid, "Проверка запущена. Ход проверки появится отдельным сообщением.")

    @router.callback_query(F.data == "filters")
    async def filters(query: CallbackQuery):
        if not await authorized(query):
            return
        await query.answer()
        await nav.show(query.from_user.id, filters_text(await db.user(query.from_user.id), settings.parser),
                                   parse_mode="HTML", reply_markup=filter_menu())

    @router.callback_query(F.data.startswith("filter:"))
    async def filter_action(query: CallbackQuery, state: FSMContext):
        if not await authorized(query):
            return
        await query.answer()
        key = query.data.split(":", 1)[1]
        if key == "category":
            names = category_names(settings.parser)
            rows = [[("Все категории", "category:all")]]
            rows.extend([[(name, f"category:{index}")] for index, name in enumerate(names)])
            await nav.show(query.from_user.id, "Выберите категорию:", reply_markup=kb(*rows))
        elif key == "seller":
            await nav.show(query.from_user.id, "Выберите продавцов:", reply_markup=kb(
                [("Только частные", "seller:private")], [("Все продавцы", "seller:all")]))
        elif key in {"from", "to", "target"}:
            await state.set_state(Input.filter_value)
            await state.update_data(key=key)
            prompt = "дату ГГГГ-ММ-ДД" if key != "target" else "целое количество от 1 до 10000"
            await nav.show(query.from_user.id, f"Введите {prompt}. /cancel — отмена.")

    @router.callback_query(F.data.startswith("category:"))
    async def category(query: CallbackQuery):
        if not await authorized(query):
            return
        value = query.data.split(":", 1)[1]
        names = category_names(settings.parser)
        if value == "all":
            selected = "all"
        elif value.isdecimal() and int(value) < len(names):
            selected = names[int(value)]
        else:
            await query.answer("Категория больше недоступна.", show_alert=True)
            return
        await db.update_filter(query.from_user.id, "category", selected)
        await query.answer("Сохранено")
        await nav.show(query.from_user.id, filters_text(await db.user(query.from_user.id), settings.parser), parse_mode="HTML", reply_markup=filter_menu())

    @router.callback_query(F.data.startswith("seller:"))
    async def seller(query: CallbackQuery):
        if not await authorized(query):
            return
        value = query.data.split(":", 1)[1]
        if value not in {"private", "all"}:
            await query.answer("Неверный выбор.", show_alert=True)
            return
        await db.update_filter(query.from_user.id, "seller_mode", value)
        await query.answer("Сохранено")
        await nav.show(query.from_user.id, filters_text(await db.user(query.from_user.id), settings.parser), parse_mode="HTML", reply_markup=filter_menu())

    @router.message(Input.filter_value)
    async def filter_value(message: Message, state: FSMContext):
        user = await db.user(message.from_user.id)
        if not active(user, settings.admin_id):
            await state.clear()
            await nav.show(message.from_user.id, "Нет активного доступа.")
            return
        key = (await state.get_data())["key"]
        try:
            if key == "target":
                if not message.text or not message.text.isdecimal() or not 1 <= int(message.text) <= 10000:
                    raise ValueError("Введите целое число от 1 до 10000.")
                value, column = int(message.text), "target"
            else:
                value = validate_date((message.text or "").strip())
                column = "registered_from" if key == "from" else "registered_to"
            candidate = {**user, column: value}
            validate_filters(settings.parser, candidate)
        except ValueError as exc:
            await nav.show(message.from_user.id, str(exc) + " Повторите ввод или /cancel.")
            return
        await db.update_filter(message.from_user.id, column, value)
        await state.clear()
        await nav.show(message.from_user.id, filters_text(await db.user(message.from_user.id), settings.parser),
                             parse_mode="HTML", reply_markup=filter_menu())

    @router.callback_query(F.data == "admin")
    async def admin(query: CallbackQuery):
        if not await authorized(query, admin=True):
            return
        await query.answer()
        await nav.show(query.from_user.id, "🛡 Управление ботом", reply_markup=admin_menu())

    @router.callback_query(F.data.startswith("admin:"))
    async def admin_action(query: CallbackQuery, state: FSMContext):
        if not await authorized(query, admin=True):
            return
        await query.answer()
        action = query.data.split(":", 1)[1]
        if action == "ready":
            if query.message and query.message.text and "Войдите вручную" in query.message.text:
                try:
                    await service.bot.delete_message(chat_id=query.from_user.id,
                                                     message_id=query.message.message_id)
                except (TelegramBadRequest, TelegramForbiddenError):
                    pass
            if service.login_ready and service.login_waiting:
                service.login_ready.set()
                await nav.show(query.from_user.id, "Продолжаю проверку входа.")
            else:
                await nav.show(query.from_user.id, "Сейчас нет ожидания входа.")
        elif action == "login":
            if service.login_ready:
                await nav.show(query.from_user.id, "Браузер уже ждёт входа.")
            else:
                if service.launch_login():
                    await nav.show(query.from_user.id, "Открываю OLX на компьютере, где запущен бот.")
                else:
                    await nav.show(query.from_user.id, "Браузер занят проверкой или входом. Попробуйте позже.")
        elif action == "users" or action.startswith("users:"):
            offset = 0 if action == "users" else action.split(":", 1)[1]
            if not str(offset).isdecimal() or int(offset) > 1000000:
                await nav.show(query.from_user.id, "Неверная страница.")
                return
            offset = int(offset)
            users = await db.all("SELECT * FROM users ORDER BY joined_at DESC LIMIT 30 OFFSET ?", (offset,))
            lines = [f"{u['tg_id']} @{html.escape(u['username'] or '—')} · "
                     f"{u['expires_at'] or ('∞' if u['lifetime'] else 'нет доступа')}" for u in users]
            page = kb([("Ещё", f"admin:users:{offset+30}")]) if len(users) == 30 else None
            await nav.show(query.from_user.id, "Пользователи:\n" + ("\n".join(lines) or "Пусто"),
                                       parse_mode="HTML", reply_markup=page)
        elif action == "stats":
            users = await db.all("SELECT * FROM users")
            await nav.show(query.from_user.id, f"Всего: {len(users)}\nАктивных: {sum(active(u, settings.admin_id) for u in users)}\n"
                                       f"Заблокированных: {sum(u['blocked'] for u in users)}\n"
                                       f"Просроченных: {sum(bool(u['expires_at']) and not active(u, settings.admin_id) for u in users)}\n"
                                       f"Интервал: {await db.option('interval_minutes')} минут")
        elif action == "broadcast":
            await state.set_state(Input.broadcast)
            await nav.show(query.from_user.id, "Введите текст рассылки до 4000 символов. /cancel — отмена.")
        elif action == "interval":
            await state.set_state(Input.interval)
            await nav.show(query.from_user.id, "Введите интервал автопроверки в минутах (от 5 до 1440).")
        else:
            await state.set_state(Input.admin_user)
            await state.update_data(action=action)
            await nav.show(query.from_user.id, "Введите @username пользователя, уже нажавшего /start, или Telegram ID.")

    @router.message(Input.admin_user)
    async def admin_user(message: Message, state: FSMContext):
        if message.from_user.id != settings.admin_id:
            await state.clear()
            return
        data = await state.get_data()
        user = await db.resolve(message.text or "")
        if not user:
            await nav.show(message.from_user.id, "Не найден. Пользователь должен сначала нажать /start. Повторите или /cancel.")
            return
        action = data["action"]
        if action == "find":
            await nav.show(message.from_user.id, profile_text(user, settings.admin_id), parse_mode="HTML")
        elif action in {"revoke", "block", "unblock"}:
            if user["tg_id"] == settings.admin_id:
                await nav.show(message.from_user.id, "Владельца нельзя блокировать или лишить доступа.")
                await state.clear()
                return
            if action == "revoke":
                await db.revoke(user["tg_id"])
                await service.stop(user["tg_id"])
            else:
                await db.execute("UPDATE users SET blocked=? WHERE tg_id=?", (int(action == "block"), user["tg_id"]))
                if action == "block":
                    await service.stop(user["tg_id"])
            await nav.show(message.from_user.id, "Готово: " + profile_text(await db.user(user["tg_id"]), settings.admin_id), parse_mode="HTML")
        else:
            await state.update_data(tg_id=user["tg_id"])
            await state.set_state(Input.duration)
            await nav.show(message.from_user.id, "Выберите срок:", reply_markup=kb(
                [("1 день", "duration:1"), ("3 дня", "duration:3")],
                [("7 дней", "duration:7"), ("14 дней", "duration:14")],
                [("30 дней", "duration:30"), ("90 дней", "duration:90")],
                [("Навсегда", "duration:forever"), ("Другой срок", "duration:custom")] ))
            return
        await state.clear()

    @router.callback_query(F.data.startswith("duration:"))
    async def duration_button(query: CallbackQuery, state: FSMContext):
        if not await authorized(query, admin=True):
            return
        if await state.get_state() != Input.duration.state:
            await query.answer("Выбор срока устарел.", show_alert=True)
            return
        await query.answer()
        value = query.data.split(":", 1)[1]
        if value == "custom":
            await nav.show(query.from_user.id, "Введите, например, 12 часов или 45 дней.")
        else:
            seconds = None if value == "forever" else int(value) * 86400
            await finish_grant(query.from_user.id, state, seconds)

    async def finish_grant(admin_id, state, seconds):
        data = await state.get_data()
        uid = data["tg_id"]
        if data["action"] == "extend":
            user = await db.user(uid)
            if not active(user) or user["lifetime"]:
                await nav.show(admin_id, "Продлевать можно только действующую срочную подписку.")
                await state.clear()
                return
        result = await db.grant(uid, seconds)
        await state.clear()
        await nav.show(admin_id, "Доступ сохранён: " + profile_text(result, settings.admin_id), parse_mode="HTML")
        await service.send(uid, "Вам открыт или продлён доступ к парсеру OLX.")

    @router.message(Input.duration)
    async def duration_text(message: Message, state: FSMContext):
        if message.from_user.id != settings.admin_id:
            await state.clear()
            return
        try:
            seconds = parse_duration(message.text or "")
        except ValueError as exc:
            await nav.show(message.from_user.id, str(exc))
            return
        await finish_grant(message.from_user.id, state, seconds)

    @router.message(Input.interval)
    async def interval(message: Message, state: FSMContext):
        if message.from_user.id != settings.admin_id:
            await state.clear()
            return
        value = (message.text or "").strip()
        if not value.isdecimal() or not 5 <= int(value) <= 1440:
            await nav.show(message.from_user.id, "Введите целое число минут от 5 до 1440.")
            return
        await db.set_option("interval_minutes", value)
        await state.clear()
        await nav.show(message.from_user.id, f"Интервал: {value} минут.")

    @router.message(Input.broadcast)
    async def broadcast(message: Message, state: FSMContext):
        if message.from_user.id != settings.admin_id:
            await state.clear()
            return
        content = message.text or ""
        if not 1 <= len(content) <= 4000:
            await nav.show(message.from_user.id, "Нужен текст от 1 до 4000 символов.")
            return
        users = await db.all("SELECT tg_id FROM users WHERE blocked=0")
        delivered = 0
        for user in users:
            delivered += await service.send(user["tg_id"], content)
        await state.clear()
        await nav.show(message.from_user.id, f"Рассылка завершена: {delivered} из {len(users)}.")

    return router
