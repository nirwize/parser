from __future__ import annotations

import asyncio
import html
import logging
import threading
from dataclasses import replace
from datetime import datetime, timedelta

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from olx_parser.browser import Scanner
from olx_parser.core import REASONS, advert_id
from .database import active, now
from .filters import validate_filters
from .keyboards import kb


log = logging.getLogger(__name__)


def listing_text(item):
    return (f"<b>{html.escape(item.title)}</b>\n"
            f"Цена: {html.escape(item.price_eur)} €\n"
            f"Категория: {html.escape(item.category)}\n"
            f"Телефон: наличие подтверждено\n"
            f'<a href="{html.escape(item.url, quote=True)}">Открыть объявление</a>')


def progress_text(checked=0, accepted=0, reason=None, summary=None, delivered=0):
    lines = ["🔎 Ход проверки", f"Проверено объявлений: {checked}",
             f"Подошло под фильтры: {accepted}"]
    if reason:
        lines.append(f"Последнее объявление: #{checked} — {REASONS.get(reason, 'пропущено')}")
    if summary is None:
        lines.append("Статус: проверка идёт" if checked else "Статус: подготовка браузера")
    else:
        status = {"complete": "завершена", "stopped": "остановлена",
                  "blocked": "доступ к OLX ограничен", "error": "ошибка",
                  "incomplete": "не удалось проверить объявления"}.get(summary.status, summary.status)
        lines.extend((f"Статус: {status}", f"Новых отправлено: {delivered}", summary.message))
    return "\n".join(lines)


class ScanService:
    def __init__(self, db, settings, bot):
        self.db, self.settings, self.bot = db, settings, bot
        self.lock = asyncio.Lock()
        self.current_id = None
        self.stop_event = None
        self.login_ready = None
        self.login_waiting = False
        self.session_state = None
        self.tasks: set[asyncio.Task] = set()
        # Reserve the browser before create_task yields to the event loop.
        self.reservation = None

    def busy(self):
        return self.lock.locked() or self.reservation is not None

    def launch(self, user_id, *, notify=True):
        if self.busy():
            return None
        self.reservation = ("scan", user_id)
        task = asyncio.create_task(self.run(user_id, notify=notify))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def launch_login(self):
        if self.busy():
            return None
        self.reservation = ("login", self.settings.admin_id)
        task = asyncio.create_task(self.login())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def send(self, user_id, text, **kwargs):
        for attempt in range(2):
            try:
                await self.bot.send_message(user_id, text, **kwargs)
                return True
            except TelegramRetryAfter as exc:
                if attempt == 0 and exc.retry_after <= 30:
                    await asyncio.sleep(exc.retry_after)
                    continue
                log.warning("Telegram rate limit for user %s", user_id)
            except TelegramForbiddenError:
                await self.db.execute("UPDATE users SET running=0 WHERE tg_id=?", (user_id,))
            except Exception:
                log.exception("Telegram delivery failed for user %s", user_id)
            break
        return False

    async def start_progress(self, user_id):
        try:
            return await self.bot.send_message(user_id, progress_text())
        except TelegramForbiddenError:
            await self.db.execute("UPDATE users SET running=0 WHERE tg_id=?", (user_id,))
        except Exception:
            log.exception("Could not create progress message for user %s", user_id)
        return None

    async def edit_progress(self, user_id, message_id, text):
        for attempt in range(2):
            try:
                await self.bot.edit_message_text(text, chat_id=user_id, message_id=message_id)
                return True
            except TelegramRetryAfter as exc:
                if attempt == 0 and exc.retry_after <= 30:
                    await asyncio.sleep(exc.retry_after)
                    continue
                log.warning("Telegram edit rate limit for user %s", user_id)
            except TelegramBadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    log.warning("Could not edit progress for user %s: %s", user_id, exc)
            except TelegramForbiddenError:
                await self.db.execute("UPDATE users SET running=0 WHERE tg_id=?", (user_id,))
            except Exception:
                log.exception("Could not edit progress for user %s", user_id)
            break
        return False

    async def progress_worker(self, user_id, message_id, queue):
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                checked, accepted, reason = item
                await self.edit_progress(user_id, message_id,
                                         progress_text(checked, accepted, reason))
            finally:
                queue.task_done()

    async def run(self, user_id, *, notify=True):
        try:
            user = await self.db.user(user_id)
            if not active(user, self.settings.admin_id):
                if notify:
                    await self.send(user_id, "Нет активного доступа к парсеру.")
                return
            try:
                config = validate_filters(self.settings.parser, user)
            except ValueError as exc:
                if notify:
                    await self.send(user_id, f"Проверьте фильтры: {html.escape(str(exc))}")
                return
            if self.lock.locked():
                if notify:
                    await self.send(user_id, "Парсер занят другой проверкой. Попробуйте позже.")
                return
            async with self.lock:
                self.reservation = None
                self.current_id = user_id
                self.stop_event = threading.Event()
                needs_login = self.session_state is None
                if needs_login:
                    self.login_ready = threading.Event()
                progress = await self.start_progress(user_id)
                queue = asyncio.Queue(maxsize=1)
                worker = (asyncio.create_task(self.progress_worker(user_id, progress.message_id, queue))
                          if progress else None)
                loop = asyncio.get_running_loop()
                last_reason = [None]

                def on_login_required():
                    self.login_waiting = True
                    async def prompt():
                        if progress:
                            await self.edit_progress(user_id, progress.message_id,
                                                     "🔎 Ход проверки\nСтатус: ожидание ручного входа в OLX на компьютере с ботом.")
                        await self.send(self.settings.admin_id,
                                        "Открыт браузер OLX на компьютере с ботом. Войдите вручную, затем нажмите кнопку. Проверка продолжится в том же браузере.",
                                        reply_markup=kb([("Я вошёл в OLX — продолжить", "admin:ready")]))
                    asyncio.run_coroutine_threadsafe(prompt(), loop)

                def on_decision(checked, accepted, reason):
                    if not worker:
                        return
                    def update():
                        last_reason[0] = reason
                        if queue.full():
                            queue.get_nowait()
                            queue.task_done()
                        queue.put_nowait((checked, accepted, reason))
                    loop.call_soon_threadsafe(update)

                items = []
                states = []
                def on_result(item):
                    items.append(item)
                try:
                    # A browser on the host PC is shared serially; subscriber preferences stay independent.
                    summary = await asyncio.to_thread(
                        Scanner(config, stop=self.stop_event, on_result=on_result,
                                on_decision=on_decision, session_state=self.session_state,
                                login_ready=self.login_ready if needs_login else None,
                                on_login_required=on_login_required if needs_login else None,
                                on_session_state=states.append).run)
                    if states:
                        self.session_state = states[-1]
                    if worker:
                        await queue.join()
                    await self.db.record_run(user_id, summary)
                    delivered = 0
                    if active(await self.db.user(user_id), self.settings.admin_id):
                        for item in items:
                            key = advert_id(item.url)
                            if await self.db.claim_delivery(user_id, key):
                                if await self.send(user_id, listing_text(item), parse_mode="HTML"):
                                    delivered += 1
                                    await self.db.execute("UPDATE users SET found_count=found_count+1 WHERE tg_id=?", (user_id,))
                                else:
                                    await self.db.unclaim_delivery(user_id, key)
                    final = progress_text(summary.checked, summary.accepted, last_reason[0],
                                          summary, delivered)
                    if progress:
                        await self.edit_progress(user_id, progress.message_id, final)
                    elif notify or summary.status in {"blocked", "error"}:
                        await self.send(user_id, final)
                    if summary.status == "blocked":
                        self.session_state = None
                        await self.db.set_option("scan_blocked", "1")
                        if user_id != self.settings.admin_id:
                            await self.send(self.settings.admin_id, f"OLX ограничил доступ: {summary.message} Автопроверки приостановлены.")
                    elif summary.status == "complete" and summary.checked:
                        await self.db.set_option("scan_blocked", "0")
                except Exception:
                    log.exception("Scan failed for user %s", user_id)
                    if progress:
                        await queue.join()
                        await self.edit_progress(user_id, progress.message_id,
                                                 "🔎 Ход проверки\nСтатус: технический сбой. Подробности в журнале бота.")
                    elif notify:
                        await self.send(user_id, "Технический сбой проверки. Подробности в журнале бота.")
                finally:
                    if worker:
                        await queue.put(None)
                        await worker
                    self.current_id = None
                    self.stop_event = None
                    if needs_login:
                        self.login_ready = None
                        self.login_waiting = False
        finally:
            if self.reservation == ("scan", user_id):
                self.reservation = None

    async def stop(self, user_id):
        await self.db.execute("UPDATE users SET running=0 WHERE tg_id=?", (user_id,))
        if self.current_id == user_id and self.stop_event:
            self.stop_event.set()

    async def login(self):
        try:
            if self.lock.locked():
                await self.send(self.settings.admin_id, "Браузер занят проверкой. Остановите её и повторите вход.")
                return
            async with self.lock:
                self.reservation = None
                self.stop_event = threading.Event()
                self.login_ready = threading.Event()
                config = replace(self.settings.parser, target=1, max_ads_per_run=1, headless=False)
                states = []
                loop = asyncio.get_running_loop()
                def opened():
                    self.login_waiting = True
                    asyncio.run_coroutine_threadsafe(
                        self.send(self.settings.admin_id,
                                  "Открыт браузер OLX на компьютере с ботом. Войдите вручную и нажмите «Вошёл в OLX»."), loop)
                try:
                    summary = await asyncio.to_thread(
                        Scanner(config, stop=self.stop_event, login_ready=self.login_ready,
                                on_login_required=opened, on_session_state=states.append,
                                login_only=True).run)
                    if states:
                        self.session_state = states[-1]
                        await self.send(self.settings.admin_id,
                                        "Сессия OLX сохранена только в памяти процесса. После перезапуска бота войдите снова.")
                    if summary.status in {"blocked", "error"}:
                        await self.send(self.settings.admin_id, f"Вход/проверка: {summary.message}")
                finally:
                    self.login_ready = None
                    self.login_waiting = False
                    self.stop_event = None
        finally:
            if self.reservation == ("login", self.settings.admin_id):
                self.reservation = None

    async def scheduler(self):
        while True:
            try:
                users = await self.db.all("SELECT * FROM users WHERE running=1")
                for user in users:
                    if not active(user, self.settings.admin_id):
                        await self.stop(user["tg_id"])
                        continue
                    interval = int(await self.db.option("interval_minutes") or 15)
                    last = await self.db.one("SELECT finished_at FROM scan_runs WHERE tg_id=? ORDER BY id DESC LIMIT 1", (user["tg_id"],))
                    due = not last or datetime.fromisoformat(last["finished_at"]) + timedelta(minutes=interval) <= now()
                    if due and self.session_state is not None and await self.db.option("scan_blocked") != "1":
                        if self.launch(user["tg_id"], notify=False):
                            break
                await asyncio.sleep(15)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Scheduler failed")
                await asyncio.sleep(60)

    async def reminders(self):
        while True:
            try:
                users = await self.db.all("SELECT * FROM users WHERE expires_at IS NOT NULL AND lifetime=0")
                for user in users:
                    end = datetime.fromisoformat(user["expires_at"])
                    if end <= now():
                        await self.stop(user["tg_id"])
                    elif end <= now() + timedelta(hours=24) and not user["reminder_sent"]:
                        if await self.send(user["tg_id"], f"Доступ закончится {end:%d.%m.%Y %H:%M} UTC."):
                            await self.db.execute("UPDATE users SET reminder_sent=1 WHERE tg_id=?", (user["tg_id"],))
                await asyncio.sleep(300)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Reminder loop failed")
                await asyncio.sleep(60)
