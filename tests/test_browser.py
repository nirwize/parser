"""Synthetic pages only: these tests do not claim OLX's live DOM is verified."""
import json
import threading
import unittest
from dataclasses import asdict, replace
from unittest.mock import patch

from olx_parser.browser import AccessBlocked, Scanner, launch_browser
from olx_parser.core import Config, evaluate


DETAIL = """<!doctype html><html><head><meta charset="utf-8"><title>Test product</title></head><body>
<nav data-testid="breadcrumbs"><a href="https://www.olx.pt/tecnologia-e-informatica/electronica/">Electrónica</a></nav>
<h1 data-cy="ad_title">Monitor de teste</h1>
<div data-testid="ad-price-container">120 €</div>
<ul data-testid="ad-parameters-container"><li><p>Particular</p></li><li>Usado</li></ul>
<div data-cy="ad_description">Monitor usado, em bom estado.</div>
<div data-testid="user-profile"><h4>SYNTHETIC SELLER</h4><p>No OLX desde janeiro de 2025</p></div>
<div data-testid="ad-contact"><a href="tel:+351200000000">Telefone</a></div>
</body></html>"""
AD1 = "https://www.olx.pt/d/anuncio/monitor-IDtest1.html"
AD2 = "https://www.olx.pt/d/anuncio/monitor-IDtest2.html"
LISTING = f'<h1>Electrónica</h1><a href="{AD1}?search_reason=promoted">Monitor</a><a href="{AD1}">Duplicate</a>'


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.config = replace(Config.load(), headless=True, delay_seconds=0, timeout_seconds=2)
        cls.pw = sync_playwright().start()
        try:
            cls.browser = launch_browser(cls.pw, cls.config)
        except Exception:
            cls.pw.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.scanner = Scanner(self.config)

    def tearDown(self):
        self.context.close()

    def extract(self, html=DETAIL):
        self.page.set_content(html)
        return self.scanner.extract(self.page)

    def test_visible_fields_pass_and_no_contacts_returned(self):
        evidence = self.extract()
        self.assertTrue(evidence["phone_confirmed"])
        serialized = json.dumps(evidence)
        self.assertNotIn("200000000", serialized)
        self.assertNotIn("SYNTHETIC SELLER", serialized)
        item, reason = evaluate(evidence, AD1, self.config.categories[0], self.config)
        self.assertEqual(reason, "accepted")
        serialized = json.dumps(asdict(item))
        for forbidden in ("200000000", "2025", "registration", "phone", "seller"):
            self.assertNotIn(forbidden, serialized)

    def test_observed_live_field_names(self):
        html = DETAIL.replace('h1 data-cy="ad_title"', 'h4 data-testid="offer_title"').replace('</h1>', '</h4>')
        html = html.replace('data-testid="user-profile"', 'data-testid="seller_card"')
        html = html.replace('data-testid="ad-contact"', 'data-testid="phones-container"')
        html = html.replace('data-cy="ad_description"', 'data-testid="ad_description"')
        evidence = self.extract(html)
        item, reason = evaluate(evidence, AD1, self.config.categories[0], self.config)
        self.assertEqual(reason, "accepted")
        self.assertIsNotNone(item)

    def test_phone_button_does_not_confirm_number(self):
        for phone in ('<button>Mostrar número</button>', '<button disabled>Mostrar número</button>',
                      '<a href="/account/">Iniciar sessão para ver telefone</a>',
                      '<a href="tel:+351200000000" style="display:none">Telefone</a>',
                      '<a href="tel:2xxxxxxxx">Telefone</a>'):
            with self.subTest(phone=phone):
                html = DETAIL.replace('<a href="tel:+351200000000">Telefone</a>', phone)
                self.assertFalse(self.extract(html)["phone_confirmed"])

    def test_footer_phone_does_not_count(self):
        html = DETAIL.replace('<div data-testid="ad-contact"><a href="tel:+351200000000">Telefone</a></div>',
                              '<footer><a href="tel:+351200000000">Apoio ao cliente</a></footer>')
        self.assertFalse(self.extract(html)["phone_confirmed"])

    def test_registration_in_description_does_not_count(self):
        html = DETAIL.replace('data-testid="user-profile"', 'data-testid="other"')
        html = html.replace("Monitor usado, em bom estado.", "No OLX desde janeiro de 2025")
        self.assertEqual(self.extract(html)["registration_labels"], [])

    def test_recommendations_do_not_supply_seller_type(self):
        html = DETAIL.replace('<p>Particular</p>', '<p>Desconhecido</p>')
        html += '<aside><p>Particular</p><a href="tel:+351200000000">Telefone</a></aside>'
        self.assertEqual(self.extract(html)["seller_types"], [])

    def test_nonphysical_or_business_offer_is_rejected(self):
        for text, key in (("Serviço de reparação de computadores", "excluded_offer"),
                          ("Licença digital de software", "excluded_offer"),
                          ("Temos stock na nossa loja física", "business_signal")):
            html = DETAIL.replace("Monitor usado, em bom estado.", text)
            self.assertTrue(self.extract(html)[key])

    def test_jsonld_breadcrumb_only(self):
        html = DETAIL.replace('data-testid="breadcrumbs"', 'data-testid="unrelated"')
        data = {"@type": "BreadcrumbList", "itemListElement": [{"item": {"@id": self.config.categories[0].url}}]}
        html += '<script type="application/ld+json">' + json.dumps(data) + '</script>'
        self.assertEqual(self.extract(html)["category_urls"], [self.config.categories[0].url])

    def test_http_403_stops_without_retry(self):
        hits = []
        def route(request):
            hits.append(request.request.url)
            request.fulfill(status=403, body="Forbidden")
        self.context.route("**/*", route)
        with self.assertRaises(AccessBlocked):
            self.scanner.navigate(self.page, self.config.categories[0].url)
        self.assertEqual(len(hits), 1)

    def test_captcha_stops(self):
        self.context.route("**/*", lambda r: r.fulfill(body="<title>Security check</title><h1>Verificação de segurança</h1>"))
        with self.assertRaises(AccessBlocked):
            self.scanner.navigate(self.page, self.config.categories[0].url)

    def test_rate_limit_waits_then_retries(self):
        hits = []
        def route(request):
            hits.append(request.request.url)
            if len(hits) == 1:
                request.fulfill(status=429, headers={"Retry-After": "12"}, body="Limited")
            else:
                request.fulfill(body=LISTING)
        self.context.route("**/*", route)
        waits = []
        self.scanner.pause = lambda seconds=0: waits.append(seconds)
        self.assertTrue(self.scanner.navigate(self.page, self.config.categories[0].url))
        self.assertIn(12, waits)
        self.assertEqual(len(hits), 2)

    def test_long_rate_limit_stops(self):
        self.context.route("**/*", lambda r: r.fulfill(status=429, headers={"Retry-After": "999"}, body="Limited"))
        with self.assertRaises(AccessBlocked):
            self.scanner.navigate(self.page, self.config.categories[0].url)

    def test_listing_links_and_pagination(self):
        category = self.config.categories[0]
        html = LISTING + f'<a href="{category.url}?page=2">Seguinte</a><a href="https://evil.example/d/anuncio/test-IDevil.html">External</a>'
        self.context.route("**/*", lambda r: r.fulfill(body=html))
        self.page.goto(category.url)
        self.assertEqual(self.scanner.links(self.page), [AD1])
        self.assertEqual(self.scanner.next_page(self.page, category), category.url + "?page=2")


class ScannerIntegrationTests(unittest.TestCase):
    def test_full_run_paginates_deduplicates_and_closes_context(self):
        # Browser routing serves a fake OLX site without contacting the internet.
        config = Config.load()
        config = replace(config, headless=True, categories=(config.categories[0],), target=2,
                         delay_seconds=0, timeout_seconds=2, max_ads_per_run=3)
        category = config.categories[0]
        hits, items, logs = [], [], []
        def setup_browser(p, c):
            real = launch_browser(p, c)
            class Wrapper:
                def new_context(self, **options):
                    ctx = real.new_context(**options)
                    def route(r):
                        url = r.request.url
                        hits.append(url)
                        if '/d/anuncio/' in url:
                            r.fulfill(body=DETAIL)
                        elif 'page=2' in url:
                            r.fulfill(body=f'<h1>Electrónica</h1><a href="{AD1}">Duplicate</a><a href="{AD2}">Another</a>')
                        else:
                            r.fulfill(body=LISTING + f'<a href="{category.url}?page=2">Seguinte</a>')
                    ctx.route("**/*", route)
                    return ctx
                def close(self):
                    self.contexts_left = len(real.contexts)
                    real.close()
            wrapper = Wrapper()
            wrappers.append(wrapper)
            return wrapper
        wrappers = []
        with patch("olx_parser.browser.launch_browser", setup_browser):
            summary = Scanner(config, log=logs.append, on_result=items.append).run()
        self.assertEqual(summary.status, "complete", summary.message)
        self.assertEqual((summary.pages, summary.checked, summary.accepted), (2, 2, 2), (summary.reasons, logs))
        self.assertEqual(len(items), 2)
        self.assertEqual(hits.count(AD1), 1)
        self.assertEqual(wrappers[0].contexts_left, 0)
        for forbidden in ("200000000", "SYNTHETIC SELLER", "janeiro"):
            self.assertNotIn(forbidden, json.dumps(logs + [asdict(i) for i in items]))


if __name__ == "__main__":
    unittest.main()
