import asyncio
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram import Router

from olx_parser.core import Config, Listing
from tg_bot.database import Database, active, now
from tg_bot.filters import parse_duration, validate_date, validate_filters
from tg_bot.handlers import setup
from tg_bot.scan_service import ScanService, listing_text


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.config = Config.load()
        self.user = dict(registered_from="2013-01-01", registered_to="2026-12-31",
                         category="all", seller_mode="all", target=1)

    def test_all_parser_filters(self):
        result = validate_filters(self.config, self.user)
        self.assertEqual(result.registered_from.year, 2013)
        self.assertEqual(result.seller_mode, "all")
        self.assertEqual(result.target, 1)
        self.assertEqual(len(result.categories), 10)
        one = validate_filters(self.config, {**self.user, "category": self.config.categories[0].name,
                                             "seller_mode": "private"})
        self.assertEqual(len(one.categories), 1)
        self.assertEqual(one.seller_mode, "private")

    def test_bad_inputs(self):
        for value in ("2025-02-30", "2025-2-1", "nonsense"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_date(value)
        for changes in ({"registered_from": "2027-01-01"}, {"category": "Несуществующая"},
                        {"seller_mode": "company"}, {"target": 0}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_filters(self.config, {**self.user, **changes})
        self.assertEqual(parse_duration("12 часов"), 43200)
        self.assertIsNone(parse_duration("навсегда"))
        with self.assertRaises(ValueError):
            parse_duration("0 дней")

    def test_message_contains_no_seller_data(self):
        item = Listing("<Товар>", "10.00", "Электроника",
                       "https://www.olx.pt/d/anuncio/test-IDtest1.html")
        text = listing_text(item)
        self.assertIn("&lt;Товар&gt;", text)
        self.assertNotIn("+351", text)


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "test.sqlite3", Config.load())
        await self.db.init()
        await self.db.register(42, "example")

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_access_expiry_extend_and_revoke(self):
        self.assertFalse(active(await self.db.user(42)))
        first = await self.db.grant(42, 86400)
        self.assertTrue(active(first))
        later = await self.db.grant(42, 86400)
        self.assertGreaterEqual(datetime.fromisoformat(later["expires_at"]) -
                                datetime.fromisoformat(first["expires_at"]), timedelta(hours=23))
        await self.db.execute("UPDATE users SET blocked=1 WHERE tg_id=42")
        self.assertFalse(active(await self.db.user(42)))
        await self.db.revoke(42)
        self.assertFalse(active(await self.db.user(42)))

    async def test_username_bound_to_id_and_delivery_dedupe(self):
        await self.db.register(43, "another")
        await self.db.register(42, "another")
        self.assertEqual((await self.db.resolve("@another"))["tg_id"], 42)
        self.assertEqual((await self.db.user(43))["username"], None)
        self.assertTrue(await self.db.claim_delivery(42, "abc"))
        self.assertFalse(await self.db.claim_delivery(42, "abc"))
        self.assertTrue(await self.db.claim_delivery(43, "abc"))

    async def test_service_local_fake_scanner(self):
        await self.db.grant(42, 86400)
        item = Listing("Тест", "99.00", "Электроника",
                       "https://www.olx.pt/d/anuncio/test-IDabc.html")
        summary = SimpleNamespace(checked=1, accepted=1, status="complete", message="Готово")
        class FakeScanner:
            def __init__(self, config, **kwargs):
                self.callback = kwargs["on_result"]
                self.decision = kwargs["on_decision"]
            def run(self):
                self.callback(item)
                self.decision(1, 1, "accepted")
                return summary
        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=10)),
                              edit_message_text=AsyncMock())
        settings = SimpleNamespace(parser=Config.load(), admin_id=999)
        service = ScanService(self.db, settings, bot)
        with patch("tg_bot.scan_service.Scanner", FakeScanner):
            await service.run(42)
            await service.run(42)
        self.assertEqual((await self.db.user(42))["found_count"], 1)
        self.assertEqual(bot.send_message.await_count, 3)  # two progress messages, item once
        self.assertGreaterEqual(bot.edit_message_text.await_count, 2)
        self.assertIn("Новых отправлено: 0", bot.edit_message_text.await_args.args[0])

    async def test_one_progress_message_is_edited_for_each_ad_and_final(self):
        await self.db.grant(42, 86400)
        seen = [threading.Event(), threading.Event()]
        summary = SimpleNamespace(checked=2, accepted=1, status="complete", message="Готово")
        class FakeScanner:
            def __init__(self, config, **kwargs):
                self.decision = kwargs["on_decision"]
            def run(self):
                self.decision(1, 0, "price")
                if not seen[0].wait(2):
                    raise AssertionError("Первое изменение не получено")
                self.decision(2, 1, "accepted")
                if not seen[1].wait(2):
                    raise AssertionError("Второе изменение не получено")
                return summary
        edits = []
        async def edit(text, **kwargs):
            edits.append((text, kwargs))
            if "#1" in text:
                seen[0].set()
            if "#2" in text:
                seen[1].set()
        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=77)),
                              edit_message_text=AsyncMock(side_effect=edit))
        service = ScanService(self.db, SimpleNamespace(parser=Config.load(), admin_id=999), bot)
        with patch("tg_bot.scan_service.Scanner", FakeScanner):
            await service.run(42)
        self.assertEqual(bot.send_message.await_count, 1)
        self.assertEqual(len(edits), 3)
        self.assertTrue(all(kwargs["message_id"] == 77 for _, kwargs in edits))
        self.assertIn("Статус: завершена", edits[-1][0])

    async def test_scan_requests_manual_login_and_clears_old_block_after_success(self):
        await self.db.grant(42, 86400)
        await self.db.set_option("scan_blocked", "1")
        summary = SimpleNamespace(checked=1, accepted=0, status="complete", message="Готово")
        observed = []
        class FakeScanner:
            def __init__(self, config, **kwargs):
                self.kwargs = kwargs
            def run(self):
                observed.append(self.kwargs)
                self.kwargs["on_login_required"]()
                if not self.kwargs["login_ready"].wait(2):
                    raise AssertionError("Ручной вход не подтверждён")
                self.kwargs["on_session_state"]({"cookies": [{"name": "session"}]})
                self.kwargs["on_decision"](1, 0, "price")
                return summary
        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=90)),
                              edit_message_text=AsyncMock())
        settings = SimpleNamespace(parser=Config.load(), admin_id=999)
        service = ScanService(self.db, settings, bot)
        with patch("tg_bot.scan_service.Scanner", FakeScanner):
            task = service.launch(42)
            for _ in range(100):
                if service.login_waiting:
                    break
                await asyncio.sleep(.01)
            self.assertTrue(service.login_waiting)
            self.assertIsNotNone(service.login_ready)
            service.login_ready.set()
            await task
        self.assertIsNone(observed[0]["session_state"])
        self.assertEqual(service.session_state, {"cookies": [{"name": "session"}]})
        self.assertEqual(await self.db.option("scan_blocked"), "0")
        self.assertFalse(service.login_waiting)

    async def test_blocked_scan_drops_stale_session_and_pauses_auto(self):
        await self.db.grant(42, 86400)
        summary = SimpleNamespace(checked=4, accepted=0, status="blocked", message="HTTP 403")
        class FakeScanner:
            def __init__(self, config, **kwargs):
                self.kwargs = kwargs
            def run(self):
                return summary
        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=91)),
                              edit_message_text=AsyncMock())
        settings = SimpleNamespace(parser=Config.load(), admin_id=999)
        service = ScanService(self.db, settings, bot)
        service.session_state = {"cookies": [{"name": "old"}]}
        with patch("tg_bot.scan_service.Scanner", FakeScanner):
            await service.run(42)
        self.assertIsNone(service.session_state)
        self.assertEqual(await self.db.option("scan_blocked"), "1")

    async def test_first_scan_reserves_browser_before_scheduler_can_launch(self):
        await self.db.grant(42, 86400)
        await self.db.execute("UPDATE users SET running=1 WHERE tg_id=42")
        calls = []
        summary = SimpleNamespace(checked=0, accepted=0, status="complete", message="Готово")
        class FakeScanner:
            def __init__(self, config, **kwargs):
                pass
            def run(self):
                calls.append(1)
                return summary
        bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=55)),
                              edit_message_text=AsyncMock())
        service = ScanService(self.db, SimpleNamespace(parser=Config.load(), admin_id=999), bot)
        with patch("tg_bot.scan_service.Scanner", FakeScanner):
            first = service.launch(42)
            self.assertIsNotNone(first)
            self.assertTrue(service.busy())
            self.assertIsNone(service.launch(42, notify=False))
            self.assertIsNone(service.launch_login())
            scheduler = asyncio.create_task(service.scheduler())
            await first
            scheduler.cancel()
            await asyncio.gather(scheduler, return_exceptions=True)
        self.assertEqual(len(calls), 1)
        self.assertFalse(service.busy())
        self.assertFalse(any("занят" in str(call) for call in bot.send_message.await_args_list))

    async def test_router_builds_with_installed_aiogram(self):
        service = SimpleNamespace()
        router = setup(self.db, SimpleNamespace(admin_id=999, parser=Config.load()), service)
        self.assertIsInstance(router, Router)


if __name__ == "__main__":
    unittest.main()
