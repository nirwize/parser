from __future__ import annotations

import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

from .core import Category, Config, Listing, REASONS, advert_id, canonical_ad_url, evaluate, olx_url


EXTRACT_JS = (Path(__file__).with_name("extract.js")).read_text(encoding="utf-8")
BLOCKED_JS = r"""() => {
  const visible = e => e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
  const challenge = [...document.querySelectorAll(
    'iframe[src*="captcha"], iframe[src*="challenge"], #captcha, [id*="challenge-running"]'
  )].some(visible);
  const headings = [...document.querySelectorAll('h1, h2')].filter(visible).map(e => e.innerText).join(' ');
  const title = document.title + ' ' + headings;
  return challenge || /access denied|acesso negado|verify you are human|verifique que|just a moment|security check|verificação de segurança|captcha/i.test(title);
}"""


class ScanError(Exception):
    """User-facing errors must contain no captured page data."""


class Stopped(ScanError):
    pass


class AccessBlocked(ScanError):
    pass


@dataclass
class Summary:
    checked: int = 0
    accepted: int = 0
    pages: int = 0
    reasons: Counter = field(default_factory=Counter)
    status: str = "complete"
    message: str = ""


def retry_after_seconds(value: str | None, now: datetime | None = None) -> float | None:
    if not value:
        return None
    try:
        if value.strip().isdigit():
            return float(value.strip())
        instant = parsedate_to_datetime(value)
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        return max(0.0, (instant - (now or datetime.now(timezone.utc))).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


def launch_browser(playwright, config: Config):
    channels = ("chrome", "msedge", "chromium") if config.browser_channel == "auto" else (config.browser_channel,)
    for channel in channels:
        try:
            options = {"headless": config.headless}
            if channel != "chromium":
                options["channel"] = channel
            # No stealth arguments, proxy rotation or access-control workarounds.
            return playwright.chromium.launch(**options)
        except Exception:
            continue
    raise ScanError("Браузер не запустился. Установите Chrome/Edge или выполните python -m playwright install chromium.")


class Scanner:
    def __init__(self, config: Config, stop: threading.Event | None = None,
                 log=None, on_result=None, on_progress=None):
        self.config = config
        self.stop = stop if stop is not None else threading.Event()
        self.log = log or (lambda message: None)
        self.on_result = on_result or (lambda result: None)
        self.on_progress = on_progress or (lambda checked, accepted: None)
        self._last_navigation = 0.0

    def pause(self, seconds: float = 0) -> None:
        if self.stop.wait(max(0, seconds)):
            raise Stopped("Сбор остановлен пользователем.")

    def navigate(self, page, url: str) -> bool:
        """Return false only for removed pages. Retry transient failures with a bound."""
        safe = olx_url(url)
        if not safe:
            raise ScanError("Переход вне публичных страниц OLX.pt запрещён.")
        from playwright.sync_api import Error as PlaywrightError
        for attempt in range(self.config.max_retries + 1):
            self.pause(self.config.delay_seconds - (time.monotonic() - self._last_navigation))
            self._last_navigation = time.monotonic()
            try:
                response = page.goto(safe, wait_until="domcontentloaded",
                                     timeout=self.config.timeout_seconds * 1000)
            except PlaywrightError:
                self.pause()
                if page.is_closed():
                    raise ScanError("Окно браузера было закрыто.") from None
                if attempt == self.config.max_retries:
                    raise ScanError("Страница не загрузилась после ограниченного числа повторов.") from None
                self.log("Ошибка соединения; выполняется повтор с задержкой.")
                self.pause(5 * (2 ** attempt))
                continue
            if not olx_url(page.url):
                raise AccessBlocked("Сайт перенаправил на вход или другой домен. Сбор остановлен.")
            if response is None:
                raise ScanError("Не получен ответ страницы.")
            status = response.status
            if status in {401, 403}:
                raise AccessBlocked(f"OLX вернул HTTP {status}. Сбор остановлен без обхода ограничений.")
            if status in {404, 410}:
                return False
            if status == 429:
                wait = retry_after_seconds(response.header_value("retry-after"))
                wait = max(wait if wait is not None else 15 * (2 ** attempt), self.config.delay_seconds)
                if attempt == self.config.max_retries or wait > 120:
                    raise AccessBlocked("OLX ограничил частоту запросов (429). Повторите запуск позже.")
                self.log(f"Ограничение частоты запросов; пауза {wait:.0f} с.")
                self.pause(wait)
                continue
            if status >= 500:
                if attempt == self.config.max_retries:
                    raise ScanError("Сервер OLX временно недоступен.")
                self.log("Временная ошибка OLX; повтор с задержкой.")
                self.pause(5 * (2 ** attempt))
                continue
            if status >= 400:
                raise ScanError(f"Не удалось открыть страницу: HTTP {status}.")
            self.check_access(page)
            return True
        raise ScanError("Не удалось загрузить страницу.")

    def check_access(self, page):
        path = urlsplit(page.url).path.casefold()
        if any(part in path for part in ("/account", "/login", "/oauth", "/authentication")):
            raise AccessBlocked("Для продолжения нужен вход. Сбор остановлен.")
        if page.evaluate(BLOCKED_JS):
            raise AccessBlocked("Обнаружена CAPTCHA или проверка доступа. Сбор остановлен.")

    def wait_for_content(self, page, selector: str) -> bool:
        # Short polling permits cancellation even while a page is being hydrated.
        from playwright.sync_api import Error as PlaywrightError
        deadline = time.monotonic() + self.config.timeout_seconds
        while time.monotonic() < deadline:
            self.pause()
            self.check_access(page)
            try:
                if page.locator(selector).first.is_visible():
                    return True
            except PlaywrightError:
                raise ScanError("Не удалось прочитать элемент страницы. Проверьте селекторы в config.json.") from None
            if page.get_by_text(re.compile(r"não encontrámos|nenhum anúncio", re.I)).count():
                return False
            self.pause(0.5)
        return False

    def extract(self, page) -> dict:
        # No screenshots, HTML dumps, HAR, storage_state, profiles or phone strings.
        return page.evaluate(EXTRACT_JS, {
            "selectors": self.config.selectors,
            "offerPatterns": list(self.config.excluded_offer_patterns),
            "businessPatterns": list(self.config.business_patterns),
        })

    def links(self, page) -> list[str]:
        raw = page.locator(self.config.selectors["listing_links"]).evaluate_all(
            "elements => elements.filter(e => e.getClientRects().length).map(e => e.getAttribute('href'))"
        )
        urls = [canonical_ad_url(value, page.url) for value in raw if value]
        return list(dict.fromkeys(url for url in urls if url))

    def next_page(self, page, category: Category) -> str | None:
        links = page.locator(self.config.selectors["next_link"])
        if not links.count():
            links = page.get_by_role("link", name="Seguinte", exact=True)
        for i in range(links.count()):
            link = links.nth(i)
            if not link.is_visible() or link.get_attribute("aria-disabled") == "true":
                continue
            href = link.get_attribute("href")
            safe = olx_url(href, page.url) if href else None
            if safe and urlsplit(safe).path.rstrip("/") == urlsplit(category.url).path.rstrip("/"):
                return safe
        return None

    def run(self) -> Summary:
        summary = Summary()
        browser = None
        context = None
        try:
            from playwright.sync_api import sync_playwright, Error as PlaywrightError
        except ImportError:
            summary.status = "error"
            summary.message = "Установите зависимости: python -m pip install -r requirements.txt"
            return summary
        try:
            with sync_playwright() as p:
                try:
                    self.pause()
                    browser = launch_browser(p, self.config)
                    # A fresh, nonpersistent context: authentication is not read or saved.
                    context = browser.new_context(locale="pt-PT", viewport={"width": 1400, "height": 900})
                    context.set_default_timeout(self.config.timeout_seconds * 1000)
                    listing_page = context.new_page()
                    detail_page = context.new_page()
                    seen_ads = set()
                    for category in self.config.categories:
                        if not category.enabled:
                            continue
                        self.pause()
                        if summary.accepted >= self.config.target or summary.checked >= self.config.max_ads_per_run:
                            break
                        self.log(f"Категория: {category.name}")
                        next_url = category.url
                        seen_pages = set()
                        for _ in range(self.config.max_pages_per_category):
                            if not next_url or next_url in seen_pages:
                                break
                            seen_pages.add(next_url)
                            if not self.navigate(listing_page, next_url):
                                self.log("Категория недоступна; проверьте её URL в config.json.")
                                break
                            summary.pages += 1
                            if not self.wait_for_content(listing_page, self.config.selectors["listing_links"]):
                                self.log("Ссылки на объявления не найдены: пустая выдача или изменившаяся разметка.")
                                break
                            urls = self.links(listing_page)
                            if not urls:
                                self.log("Нет доступных ссылок OLX.pt в этой выдаче.")
                                break
                            for url in urls:
                                self.pause()
                                if summary.accepted >= self.config.target or summary.checked >= self.config.max_ads_per_run:
                                    break
                                key = advert_id(url)
                                if key in seen_ads:
                                    continue
                                seen_ads.add(key)
                                summary.checked += 1
                                result = None
                                reason = "page_error"
                                try:
                                    if not self.navigate(detail_page, url):
                                        reason = "unavailable"
                                    elif not self.wait_for_content(detail_page, self.config.selectors["title"]):
                                        reason = "title"
                                    else:
                                        # Allow the visible seller/contact fields to hydrate. Not a network-idle wait.
                                        self.pause(1)
                                        self.check_access(detail_page)
                                        evidence = self.extract(detail_page)
                                        if canonical_ad_url(detail_page.url) != url:
                                            reason = "url"
                                        else:
                                            result, reason = evaluate(evidence, url, category, self.config)
                                        del evidence
                                except (AccessBlocked, Stopped):
                                    raise
                                except ScanError as exc:
                                    self.log(str(exc))
                                    if detail_page.is_closed():
                                        raise
                                except PlaywrightError:
                                    if detail_page.is_closed():
                                        raise ScanError("Окно браузера было закрыто.") from None
                                    self.log("Не удалось прочитать карточку; возможное изменение разметки.")
                                summary.reasons[reason] += 1
                                if result:
                                    summary.accepted += 1
                                    self.on_result(result)
                                else:
                                    self.log(f"Пропуск: {REASONS[reason]}.")
                                self.on_progress(summary.checked, summary.accepted)
                            if summary.accepted >= self.config.target or summary.checked >= self.config.max_ads_per_run:
                                break
                            next_url = self.next_page(listing_page, category)
                    if summary.accepted >= self.config.target:
                        summary.message = "Набрано заданное количество объявлений."
                    elif summary.checked >= self.config.max_ads_per_run:
                        summary.message = "Достигнут лимит проверяемых объявлений."
                    else:
                        summary.message = "Обход завершён в пределах настроенных страниц и категорий."
                    if summary.checked == 0:
                        summary.status = "incomplete"
                        summary.message = "Ни одно объявление не проверено. Проверьте доступ к OLX и селекторы."
                finally:
                    # Close before leaving Playwright, including on cancellation or access failure.
                    if context is not None:
                        try:
                            context.close()
                        except PlaywrightError:
                            pass
                    if browser is not None:
                        try:
                            browser.close()
                        except PlaywrightError:
                            pass
        except Stopped as exc:
            summary.status, summary.message = "stopped", str(exc)
        except AccessBlocked as exc:
            summary.status, summary.message = "blocked", str(exc)
        except ScanError as exc:
            summary.status, summary.message = "error", str(exc)
        except Exception:
            # Raw exceptions may contain DOM text or URLs; never echo them to logs.
            summary.status = "error"
            summary.message = "Сбор прерван технической ошибкой. Проверьте зависимости, браузер и селекторы."
        return summary
