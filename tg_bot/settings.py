from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from olx_parser.core import Config


HERE = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    token: str
    admin_id: int
    database: Path
    parser: Config

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(HERE / ".env")
        token = os.getenv("BOT_TOKEN", "").strip()
        admin = os.getenv("ADMIN_ID", "").strip()
        if not token or not admin.isdecimal() or int(admin) <= 0:
            raise ValueError("Укажите BOT_TOKEN и числовой ADMIN_ID в tg_bot/.env")
        return cls(token, int(admin), HERE / "bot.sqlite3", Config.load())
