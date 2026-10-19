from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from .database import Database
from .handlers import setup
from .scan_service import ScanService
from .settings import Settings


async def main():
    logging.basicConfig(level=logging.INFO)
    settings = Settings.load()
    db = Database(settings.database, settings.parser)
    await db.init()
    # OLX login state lives only in memory; a restarted bot must request login again.
    await db.execute("UPDATE users SET running=0")
    bot = Bot(settings.token)
    service = ScanService(db, settings, bot)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(setup(db, settings, service))
    scheduler = asyncio.create_task(service.scheduler())
    reminders = asyncio.create_task(service.reminders())
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.cancel()
        reminders.cancel()
        if service.stop_event:
            service.stop_event.set()
        await asyncio.gather(scheduler, reminders, *service.tasks, return_exceptions=True)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
