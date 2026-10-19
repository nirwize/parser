from __future__ import annotations

import asyncio

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError


class Navigation:
    """Keep one replaceable bot screen per private chat."""

    KEYBOARD_VERSION = 2

    def __init__(self, db, bot):
        self.db = db
        self.bot = bot
        self._locks = {}

    async def ensure_keyboard(self, chat_id, keyboard):
        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            previous = await self.db.keyboard_message(chat_id)
            if previous is None or await self.db.keyboard_layout(chat_id) != self.KEYBOARD_VERSION:
                if previous is not None:
                    try:
                        await self.bot.delete_message(chat_id=chat_id, message_id=previous)
                    except (TelegramBadRequest, TelegramForbiddenError):
                        pass
                message = await self.bot.send_message(
                    chat_id, "Главное меню доступно на клавиатуре ниже.", reply_markup=keyboard,
                )
                await self.db.set_keyboard_message(chat_id, message.message_id)
                await self.db.set_keyboard_layout(chat_id, self.KEYBOARD_VERSION)

    async def show(self, chat_id, text, *, reply_markup=None, parse_mode=None):
        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            previous = await self.db.ui_message(chat_id)
            if previous is not None:
                try:
                    await self.bot.delete_message(chat_id=chat_id, message_id=previous)
                except (TelegramBadRequest, TelegramForbiddenError):
                    # Telegram can refuse deletion of an old/already deleted message.
                    pass
            message = await self.bot.send_message(
                chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode,
            )
            await self.db.set_ui_message(chat_id, message.message_id)
            return message
