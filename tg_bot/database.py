from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  tg_id INTEGER PRIMARY KEY, username TEXT, joined_at TEXT NOT NULL,
  activated_at TEXT, expires_at TEXT, lifetime INTEGER NOT NULL DEFAULT 0,
  blocked INTEGER NOT NULL DEFAULT 0, running INTEGER NOT NULL DEFAULT 0,
  reminder_sent INTEGER NOT NULL DEFAULT 0, found_count INTEGER NOT NULL DEFAULT 0,
  registered_from TEXT NOT NULL, registered_to TEXT NOT NULL,
  category TEXT NOT NULL, seller_mode TEXT NOT NULL, target INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS users_username ON users(username COLLATE NOCASE) WHERE username IS NOT NULL;
CREATE TABLE IF NOT EXISTS deliveries (
  tg_id INTEGER NOT NULL, advert_id TEXT NOT NULL, sent_at TEXT NOT NULL,
  PRIMARY KEY(tg_id, advert_id)
);
CREATE TABLE IF NOT EXISTS scan_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER NOT NULL,
  finished_at TEXT NOT NULL, checked INTEGER NOT NULL, accepted INTEGER NOT NULL,
  status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS options (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ui_messages (tg_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS keyboard_messages (tg_id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS keyboard_layouts (tg_id INTEGER PRIMARY KEY, version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS saved_listings (
  tg_id INTEGER NOT NULL, advert_id TEXT NOT NULL, title TEXT NOT NULL,
  price_eur TEXT NOT NULL, category TEXT NOT NULL, url TEXT NOT NULL,
  saved_at TEXT NOT NULL, PRIMARY KEY(tg_id, advert_id)
);
CREATE INDEX IF NOT EXISTS saved_listings_recent ON saved_listings(tg_id, saved_at DESC);
"""


class Database:
    def __init__(self, path: Path, defaults):
        self.path = path
        self.defaults = defaults

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            await db.execute("INSERT OR IGNORE INTO options VALUES ('interval_minutes', '15')")
            await db.commit()

    async def one(self, sql, params=()):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def all(self, sql, params=()):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def execute(self, sql, params=()):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(sql, params)
            await db.commit()

    async def register(self, tg_id: int, username: str | None):
        username = username.casefold() if username else None
        if username:
            # Telegram usernames can be reassigned; the latest /start owns the name.
            await self.execute("UPDATE users SET username=NULL WHERE username=? COLLATE NOCASE AND tg_id<>?",
                               (username, tg_id))
        user = await self.one("SELECT * FROM users WHERE tg_id=?", (tg_id,))
        if not user:
            await self.execute("""INSERT INTO users
              (tg_id, username, joined_at, registered_from, registered_to, category, seller_mode, target)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
              (tg_id, username, iso(now()), self.defaults.registered_from.isoformat(),
               self.defaults.registered_to.isoformat(), "all", self.defaults.seller_mode,
               self.defaults.target))
        else:
            await self.execute("UPDATE users SET username=? WHERE tg_id=?", (username, tg_id))
        return await self.user(tg_id)

    async def user(self, tg_id):
        return await self.one("SELECT * FROM users WHERE tg_id=?", (tg_id,))

    async def resolve(self, query: str):
        value = query.strip().lstrip("@")
        if value.isdecimal():
            return await self.user(int(value))
        return await self.one("SELECT * FROM users WHERE username=? COLLATE NOCASE", (value,))

    async def grant(self, tg_id: int, seconds: int | None):
        user = await self.user(tg_id)
        if not user:
            raise ValueError("Пользователь сначала должен нажать /start.")
        current = now()
        if seconds is not None and seconds <= 0:
            raise ValueError("Срок должен быть больше нуля.")
        existing = datetime.fromisoformat(user["expires_at"]) if user["expires_at"] else None
        base = max(current, existing) if existing else current
        expiry = iso(base + timedelta(seconds=seconds)) if seconds else None
        await self.execute("""UPDATE users SET activated_at=?, expires_at=?, lifetime=?,
          reminder_sent=0 WHERE tg_id=?""",
          (user["activated_at"] or iso(current), expiry, int(seconds is None), tg_id))
        return await self.user(tg_id)

    async def revoke(self, tg_id):
        await self.execute("""UPDATE users SET activated_at=NULL, expires_at=NULL,
          lifetime=0, running=0, reminder_sent=0 WHERE tg_id=?""", (tg_id,))

    async def update_filter(self, tg_id, key, value):
        if key not in {"registered_from", "registered_to", "category", "seller_mode", "target"}:
            raise ValueError("Неизвестный фильтр.")
        await self.execute(f"UPDATE users SET {key}=? WHERE tg_id=?", (value, tg_id))

    async def claim_delivery(self, tg_id, advert_id):
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute("INSERT OR IGNORE INTO deliveries VALUES (?, ?, ?)",
                                      (tg_id, advert_id, iso(now())))
            await db.commit()
            return cursor.rowcount == 1

    async def unclaim_delivery(self, tg_id, advert_id):
        await self.execute("DELETE FROM deliveries WHERE tg_id=? AND advert_id=?", (tg_id, advert_id))

    async def save_listings(self, tg_id, items):
        from olx_parser.core import advert_id

        timestamp = iso(now())
        rows = [(tg_id, advert_id(item.url), item.title, item.price_eur,
                 item.category, item.url, timestamp) for item in items]
        if not rows:
            return
        async with aiosqlite.connect(self.path) as db:
            await db.executemany("""INSERT INTO saved_listings
              (tg_id, advert_id, title, price_eur, category, url, saved_at)
              VALUES (?, ?, ?, ?, ?, ?, ?)
              ON CONFLICT(tg_id, advert_id) DO UPDATE SET
                title=excluded.title, price_eur=excluded.price_eur,
                category=excluded.category, url=excluded.url""", rows)
            await db.commit()

    async def saved_count(self, tg_id):
        row = await self.one("SELECT count(*) AS total FROM saved_listings WHERE tg_id=?", (tg_id,))
        return row["total"]

    async def saved_page(self, tg_id, limit, offset):
        return await self.all("""SELECT title, price_eur, category, url, saved_at
          FROM saved_listings WHERE tg_id=? ORDER BY saved_at DESC, advert_id DESC
          LIMIT ? OFFSET ?""", (tg_id, limit, offset))

    async def record_run(self, tg_id, summary):
        await self.execute("INSERT INTO scan_runs (tg_id, finished_at, checked, accepted, status) VALUES (?, ?, ?, ?, ?)",
                           (tg_id, iso(now()), summary.checked, summary.accepted, summary.status))

    async def option(self, key):
        row = await self.one("SELECT value FROM options WHERE key=?", (key,))
        return row["value"] if row else None

    async def set_option(self, key, value):
        await self.execute("INSERT OR REPLACE INTO options VALUES (?, ?)", (key, str(value)))

    async def ui_message(self, tg_id):
        row = await self.one("SELECT message_id FROM ui_messages WHERE tg_id=?", (tg_id,))
        return row["message_id"] if row else None

    async def set_ui_message(self, tg_id, message_id):
        await self.execute("INSERT OR REPLACE INTO ui_messages VALUES (?, ?)", (tg_id, message_id))

    async def keyboard_message(self, tg_id):
        row = await self.one("SELECT message_id FROM keyboard_messages WHERE tg_id=?", (tg_id,))
        return row["message_id"] if row else None

    async def set_keyboard_message(self, tg_id, message_id):
        await self.execute("INSERT OR REPLACE INTO keyboard_messages VALUES (?, ?)", (tg_id, message_id))

    async def keyboard_layout(self, tg_id):
        row = await self.one("SELECT version FROM keyboard_layouts WHERE tg_id=?", (tg_id,))
        return row["version"] if row else None

    async def set_keyboard_layout(self, tg_id, version):
        await self.execute("INSERT OR REPLACE INTO keyboard_layouts VALUES (?, ?)", (tg_id, version))


def active(user, admin_id=None):
    if not user or user["blocked"]:
        return False
    if admin_id is not None and user["tg_id"] == admin_id:
        return True
    if user["lifetime"]:
        return True
    return bool(user["expires_at"] and datetime.fromisoformat(user["expires_at"]) > now())
