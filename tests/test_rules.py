import json
import tempfile
import threading
import unittest
from dataclasses import asdict, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from olx_parser.browser import Scanner, Stopped, retry_after_seconds
from olx_parser.core import (
    ALL_CATEGORIES, ROOT, Config, apply_search_filters, canonical_ad_url,
    euro_price, evaluate, olx_url, registration_interval,
)


class RulesTests(unittest.TestCase):
    def setUp(self):
        self.config = Config.load()
        self.category = self.config.categories[0]
        self.url = "https://www.olx.pt/d/anuncio/teste-IDabc123.html"
        self.evidence = dict(
            title="Monitor", price="120 €", seller_types=["Particular"],
            registration_labels=["No OLX desde janeiro de 2025"],
            phone_confirmed=True, category_urls=[self.category.url],
            business_signal=False, description_checked=True, excluded_offer=False,
        )

    def check(self, **changes):
        return evaluate(self.evidence | changes, self.url, self.category, self.config, date(2026, 10, 18))

    def test_accepts_only_product_fields(self):
        result, reason = self.check()
        self.assertEqual(reason, "accepted")
        self.assertEqual(set(asdict(result)), {"title", "price_eur", "category", "url"})
        self.assertEqual(result.price_eur, "120.00")

    def test_missing_fields_fail_closed(self):
        for field in ("title", "price", "seller_types", "registration_labels", "phone_confirmed",
                      "category_urls", "description_checked", "excluded_offer", "business_signal"):
            with self.subTest(field=field):
                data = dict(self.evidence)
                data.pop(field)
                self.assertIsNone(evaluate(data, self.url, self.category, self.config)[0])

    def test_phone_must_be_boolean_confirmation(self):
        for value in (False, None, "true", "Mostrar número", 1):
            self.assertEqual(self.check(phone_confirmed=value)[1], "phone_unknown")

    def test_conflicting_or_professional_seller(self):
        for value in (["Particular", "Profissional"], ["Empresa"], ["Particular", "unknown"]):
            self.assertIsNone(self.check(seller_types=value)[0])
        self.assertEqual(self.check(business_signal=True)[1], "business")

    def test_all_sellers_includes_private_and_business_without_changing_other_rules(self):
        config = apply_search_filters(self.config, "2025-01-01", "2026-12-31",
                                      ALL_CATEGORIES, "Все продавцы")
        self.assertEqual(config.seller_mode, "all")
        self.assertEqual(len(config.categories), sum(item.enabled for item in self.config.categories))
        for types, business in ((["Particular"], False), (["Profissional"], True),
                                (["Empresa"], True), ([], False)):
            with self.subTest(types=types):
                evidence = self.evidence | {"seller_types": types, "business_signal": business}
                item, reason = evaluate(evidence, self.url, self.category, config, date(2026, 10, 18))
                self.assertEqual(reason, "accepted")
                self.assertIsNotNone(item)
        self.assertEqual(evaluate(self.evidence | {"phone_confirmed": False},
                                  self.url, self.category, config)[1], "phone_unknown")
        self.assertEqual(evaluate(self.evidence | {"excluded_offer": True},
                                  self.url, self.category, config)[1], "offer")

    def test_ui_period_and_category_limit_results(self):
        chosen = self.config.categories[1]
        config = apply_search_filters(self.config, "2025-04-01", "2025-12-31",
                                      chosen.name, "Только частные")
        self.assertEqual((config.registered_from, config.registered_to),
                         (date(2025, 4, 1), date(2025, 12, 31)))
        self.assertEqual(config.categories, (chosen,))
        self.assertEqual(config.seller_mode, "private")
        evidence = self.evidence | {
            "registration_labels": ["No OLX desde maio de 2025"],
            "category_urls": [chosen.url],
        }
        self.assertEqual(evaluate(evidence, self.url, chosen, config)[1], "accepted")
        self.assertEqual(evaluate(self.evidence, self.url, chosen, config)[1], "registration_range")

    def test_ui_invalid_dates_category_and_seller(self):
        valid = ("2025-01-01", "2026-12-31", ALL_CATEGORIES, "Только частные")
        for values in (("", *valid[1:]), ("2025-02-30", *valid[1:]),
                       ("2025/01/01", *valid[1:]), ("2027-01-01", "2026-12-31", *valid[2:]),
                       (valid[0], "", *valid[2:]), (valid[0], "2026-13-01", *valid[2:]),
                       (*valid[:2], "Несуществующая категория", valid[3]),
                       (*valid[:3], "Магазины")):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    apply_search_filters(self.config, *values)
        disabled = replace(self.config, categories=(replace(self.category, enabled=False), *self.config.categories[1:]))
        with self.assertRaises(ValueError):
            apply_search_filters(disabled, valid[0], valid[1], self.category.name, valid[3])

    def test_registration_bounds(self):
        for label in ("dezembro de 2024", "janeiro de 2027"):
            self.assertEqual(self.check(registration_labels=["No OLX desde " + label])[1], "registration_range")
        for label in ("1 de janeiro de 2025", "31 de dezembro de 2026"):
            data = self.evidence | {"registration_labels": ["No OLX desde " + label]}
            self.assertIsNotNone(evaluate(data, self.url, self.category, self.config, date(2027, 1, 1))[0])

    def test_registration_not_publication_or_last_seen(self):
        for label in ("Publicado 10 de maio de 2025", "Esteve online hoje", "Vendedor há 1 ano", "2025"):
            self.assertEqual(self.check(registration_labels=[label])[1], "registration_unknown")

    def test_registration_ambiguous(self):
        self.assertEqual(self.check(registration_labels=["No OLX desde maio de 2025", "No OLX desde junho de 2025"])[1], "registration_unknown")

    def test_month_is_interval_not_invented_day(self):
        self.assertEqual(registration_interval("No OLX desde março de 2025"), (date(2025, 3, 1), date(2025, 3, 31)))
        config = replace(self.config, registered_from=date(2025, 1, 15))
        self.assertEqual(evaluate(self.evidence, self.url, self.category, config)[1], "registration_range")
        self.assertIsNone(registration_interval("No OLX desde 31 de fevereiro de 2025"))

    def test_future_registration_rejected(self):
        self.assertEqual(self.check(registration_labels=["No OLX desde novembro de 2026"])[1], "registration_range")

    def test_price_formats(self):
        for text, expected in (("1.250,50 €", "1250.50"), ("1 250 € Negociável", "1250"), ("19,9 EUR", "19.9"), ("0 €", "0")):
            self.assertEqual(euro_price(text), Decimal(expected))
        for text in ("Sob Orçamento", "Troca", "Grátis", "€", "-5 €", "1.2 €", "120", "NaN €", "10 € / mês"):
            self.assertIsNone(euro_price(text))

    def test_non_goods_and_unknown_category_rejected(self):
        self.assertEqual(self.check(excluded_offer=True)[1], "offer")
        self.assertEqual(self.check(category_urls=["https://www.olx.pt/imoveis/"])[1], "category")
        self.assertEqual(self.check(category_urls=[self.category.url, "https://www.olx.pt/desporto-e-lazer/bicicletas/"])[1], "category")

    def test_no_contact_in_title_output(self):
        result, _ = self.check(title="Monitor +351 912 345 678 test@example.invalid")
        self.assertNotIn("912", result.title)
        self.assertNotIn("@", result.title)

    def test_urls_and_duplicate_tracking(self):
        self.assertEqual(canonical_ad_url(self.url + "?search_reason=promoted#x"), self.url)
        for url in ("https://evil.example/d/anuncio/x-IDabc.html", "https://www.olx.pt.evil.example/x", "https://u:p@www.olx.pt/x", "http://www.olx.pt/x", "https://www.olx.pt/api/partner/adverts", "https://www.olx.pt/%61pi/x"):
            self.assertIsNone(olx_url(url))
        self.assertIsNone(canonical_ad_url("https://www.olx.pt/d/anuncio/912345678-IDabc.html"))

    def test_retry_after(self):
        now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)
        self.assertEqual(retry_after_seconds("30"), 30)
        self.assertEqual(retry_after_seconds("Sun, 18 Oct 2026 12:01:00 GMT", now), 60)
        self.assertIsNone(retry_after_seconds("garbage"))

    def test_stop_interrupts_wait(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(Stopped):
            Scanner(self.config, stop).pause(60)

    def test_invalid_config_is_rejected(self):
        raw = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        for changes in ({"delay_seconds": 0}, {"target": True}, {"headless": "false"},
                        {"registered_to": "2020-01-01"}, {"max_retries": 100}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "config.json"
                path.write_text(json.dumps(raw | changes), encoding="utf-8")
                with self.assertRaises(ValueError):
                    Config.load(path)


if __name__ == "__main__":
    unittest.main()
