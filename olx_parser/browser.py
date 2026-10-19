from __future__ import annotations

import json
import re
import sys
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


class NavigationError(ScanError):
    pass


class ReadError(ScanError):
    pass


def read_error_kind(exc: Exception) -> str:
    """Classify browser failures without logging page contents or contact data."""
    message = str(exc).casefold()
    if any(term in message for term in ("execution context was destroyed", "cannot find context",
                                        "context was destroyed", "frame was detached", "navigation")):
        return "page_changed"
    if any(term in message for term in ("target closed", "page closed", "browser has been closed")):
        return "page_closed"
    if any(term in message for term in ("syntaxerror", "typeerror", "referenceerror", "invalid regular expression")):
        return "extract_script"
    if "not a valid selector" in message or "failed to execute 'queryselectorall'" in message:
        return "invalid_selector"
    return "browser_read"


READ_ERROR_MESSAGES = {
    "page_changed": "Страница обновлялась во время чтения полей.",
    "page_closed": "Вкладка закрылась во время чтения полей.",
    "extract_script": "Ошибка скрипта извлечения полей.",
    "invalid_selector": "Некорректный CSS-селектор в config.json.",
    "browser_read": "Ошибка браузера при чтении полей.",
}


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


def default_browser_executable() -> str | None:
    """Ask Windows which executable handles HTTPS for the current user."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    query = ctypes.windll.shlwapi.AssocQueryStringW
    query.argtypes = (wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR,
                      wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    query.restype = ctypes.c_long
    size = wintypes.DWORD(0)
    query(0, 2, "https", None, None, ctypes.byref(size))  # ASSOCSTR_EXECUTABLE
    if not size.value:
        return None
    buffer = ctypes.create_unicode_buffer(size.value)
    if query(0, 2, "https", None, buffer, ctypes.byref(size)) != 0:
        return None
    return buffer.value or None


def browser_options(playwright, config: Config):
    options = {"headless": config.headless}
    browser_type = playwright.chromium
    if config.browser_channel == "auto":
        executable = default_browser_executable()
        if not executable:
            raise ScanError("Не удалось определить браузер Windows по умолчанию для HTTPS.")
        name = Path(executable).name.casefold()
        if name == "chrome.exe":
            options["channel"] = "chrome"
        elif name == "msedge.exe":
            options["channel"] = "msedge"
        elif name == "firefox.exe":
            browser_type = playwright.firefox
            # Stock Firefox lacks Playwright's automation transport.
            # Use the Playwright Firefox build for the selected browser family.
        elif name in {"brave.exe", "vivaldi.exe", "opera.exe", "chromium.exe"}:
            options["executable_path"] = executable
        else:
            raise ScanError(f"Браузер Windows по умолчанию ({name}) не поддерживается Playwright.")
    elif config.browser_channel != "chromium":
        options["channel"] = config.browser_channel
    return browser_type, options


def launch_browser(playwright, config: Config):
    browser_type, options = browser_options(playwright, config)
    try:
        # No stealth arguments, proxy rotation or access-control workarounds.
        return browser_type.launch(**options)
    except Exception:
        if browser_type is playwright.firefox:
            raise ScanError("Firefox Playwright не запустился. Выполните: python -m playwright install firefox") from None
        raise ScanError("Браузер по умолчанию не запустился через Playwright. Проверьте его установку или выберите browser_channel в config.json.") from None


def launch_profile(playwright, config: Config, profile_dir: Path):
    browser_type, options = browser_options(playwright, config)
    options.update(user_data_dir=str(profile_dir), locale="pt-PT",
                   viewport={"width": 1400, "height": 900})
    try:
        return browser_type.launch_persistent_context(**options)
    except Exception:
        raise ScanError("Не удалось открыть профиль браузера бота. Закройте другое окно с этим профилем или проверьте установку браузера Playwright.") from None


def restore_profile_state(context, profile_dir: Path):
    """Restore session cookies that Chromium may discard when a window closes."""
    from playwright.sync_api import Error as PlaywrightError
    saved = profile_dir / "auth_state.json"
    if not saved.is_file():
        return False
    try:
        state = json.loads(saved.read_text(encoding="utf-8"))
        cookies = state.get("cookies", [])
        if not isinstance(cookies, list):
            raise ValueError("invalid cookies")
        if cookies:
            context.add_cookies(cookies)
        origins = {}
        for origin in state.get("origins", []):
            if not isinstance(origin, dict) or not isinstance(origin.get("origin"), str):
                continue
            host = urlsplit(origin["origin"]).hostname or ""
            if host != "olx.pt" and not host.endswith(".olx.pt"):
                continue
            values = {item["name"]: item["value"] for item in origin.get("localStorage", [])
                      if isinstance(item, dict) and isinstance(item.get("name"), str)
                      and isinstance(item.get("value"), str)}
            if values:
                origins[origin["origin"]] = values
        if origins:
            # Fill missing keys before the site's scripts run; do not overwrite newer values.
            context.add_init_script("""(() => {
                const saved = %s[location.origin];
                if (!saved) return;
                for (const [key, value] of Object.entries(saved)) {
                    if (localStorage.getItem(key) === null) localStorage.setItem(key, value);
                }
            })();""" % json.dumps(origins, ensure_ascii=False))
        return True
    except (OSError, ValueError, TypeError, PlaywrightError):
        # Keep the browser usable; a successful run will replace a bad snapshot.
        return False


def save_profile_state(context, profile_dir: Path):
    """Keep Playwright storage state inside the ignored, private browser profile."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    target = profile_dir / "auth_state.json"
    temporary = profile_dir / "auth_state.tmp"
    temporary.write_text(json.dumps(context.storage_state(), ensure_ascii=False), encoding="utf-8")
    temporary.replace(target)


class Scanner:
    def __init__(self, config: Config, stop: threading.Event | None = None,
                 log=None, on_result=None, on_progress=None,
                 login_ready: threading.Event | None = None, on_login_required=None,
                 session_state: dict | None = None, on_session_state=None,
                 on_decision=None, login_only: bool = False,
                 persistent_profile: Path | None = None, on_login_complete=None):
        self.config = config
        self.stop = stop if stop is not None else threading.Event()
        self.log = log or (lambda message: None)
        self.on_result = on_result or (lambda result: None)
        self.on_progress = on_progress or (lambda checked, accepted: None)
        self.on_decision = on_decision or (lambda checked, accepted, reason: None)
        self.login_ready = login_ready
        self.on_login_required = on_login_required or (lambda: None)
        self.session_state = session_state
        self.on_session_state = on_session_state or (lambda state: None)
        self.login_only = login_only
        self.persistent_profile = persistent_profile
        self.on_login_complete = on_login_complete or (lambda: None)
        self._last_navigation = 0.0

    def pause(self, seconds: float = 0) -> None:
        if self.stop.wait(max(0, seconds)):
            raise Stopped("Сбор остановлен пользователем.")

    def navigate(self, page, url: str, fresh_on_retry: bool = False):
        """Return the loaded page, or None for a removed advert."""
        safe = olx_url(url)
        if not safe:
            raise NavigationError("Переход вне публичных страниц OLX.pt запрещён.")
        from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout
        initial_page = page
        try:
            for attempt in range(self.config.max_retries + 1):
                self.pause(self.config.delay_seconds - (time.monotonic() - self._last_navigation))
                self._last_navigation = time.monotonic()
                try:
                    # Response headers are enough here; the DOM is awaited separately.
                    response = page.goto(safe, wait_until="commit",
                                         timeout=self.config.timeout_seconds * 1000)
                except PlaywrightError as exc:
                    self.pause()
                    if page.is_closed():
                        raise NavigationError("Вкладка закрылась во время перехода.") from None
                    kind = "таймаут" if isinstance(exc, PlaywrightTimeout) else "сбой соединения"
                    if attempt == self.config.max_retries:
                        raise NavigationError(f"Не удалось открыть карточку: {kind} после повторов.") from None
                    self.log(f"Переход: {kind}; повтор после паузы.")
                    self.pause(5 * (2 ** attempt))
                else:
                    if not olx_url(page.url):
                        raise AccessBlocked("Сайт перенаправил на вход или другой домен. Сбор остановлен.")
                    if response is None:
                        raise NavigationError("Сайт не вернул ответ страницы.")
                    status = response.status
                    if status in {401, 403}:
                        raise AccessBlocked(f"OLX вернул HTTP {status}. Сбор остановлен без обхода ограничений.")
                    if status in {404, 410}:
                        return None
                    if status == 429:
                        wait = retry_after_seconds(response.header_value("retry-after"))
                        wait = max(wait if wait is not None else 15 * (2 ** attempt), self.config.delay_seconds)
                        if attempt == self.config.max_retries or wait > 120:
                            raise AccessBlocked("OLX ограничил частоту запросов (429). Повторите запуск позже.")
                        self.log(f"Ограничение частоты запросов; пауза {wait:.0f} с.")
                        self.pause(wait)
                    elif status >= 500:
                        if attempt == self.config.max_retries:
                            raise NavigationError(f"Сервер OLX вернул HTTP {status} после повторов.")
                        self.log(f"Сервер OLX вернул HTTP {status}; повтор после паузы.")
                        self.pause(5 * (2 ** attempt))
                    elif status >= 400:
                        raise NavigationError(f"Сайт вернул HTTP {status}.")
                    else:
                        return page
                if fresh_on_retry:
                    replacement = page.context.new_page()
                    page.close()
                    page = replacement
            raise NavigationError("Не удалось открыть страницу после повторов.")
        except Exception:
            if page is not initial_page and not page.is_closed():
                page.close()
            raise

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
            try:
                self.check_access(page)
                if page.locator(selector).evaluate_all(
                    "nodes => nodes.some(node => node.getClientRects().length > 0)"
                ):
                    return True
            except PlaywrightError:
                if page.is_closed():
                    raise ReadError("Вкладка закрылась при ожидании разметки.") from None
                # A newly committed document may replace its execution context.
            try:
                if page.get_by_text(re.compile(r"não encontrámos|nenhum anúncio", re.I)).count():
                    return False
            except PlaywrightError:
                pass
            self.pause(0.5)
        return False

    def extract(self, page) -> dict:
        # Advertisement evidence never includes screenshots, HTML dumps or phone strings.
        return page.evaluate(EXTRACT_JS, {
            "selectors": self.config.selectors,
            "offerPatterns": list(self.config.excluded_offer_patterns),
            "businessPatterns": list(self.config.business_patterns),
        })

    def reveal_phone(self, page) -> bool:
        """Use only the site's visible reveal button; never return the number."""
        from playwright.sync_api import Error as PlaywrightError
        label = re.compile(r"(?:mostrar|ver|exibir)\s+(?:o\s+)?(?:n[úu]mero|telefone|contacto)", re.I)
        for selector in (self.config.selectors["contact"], self.config.selectors["seller"]):
            try:
                for scope in page.locator(selector).all():
                    button = scope.get_by_role("button", name=label).first
                    if button.count() and button.is_visible() and button.is_enabled():
                        button.click(timeout=min(5000, self.config.timeout_seconds * 1000))
                        return True
            except PlaywrightError as exc:
                kind = read_error_kind(exc)
                if kind == "page_changed":
                    return True  # The click may have navigated; re-check the page without assuming a phone.
                raise ReadError(READ_ERROR_MESSAGES[kind]) from None
        return False

    def wait_for_revealed_phone(self, page) -> bool:
        """Re-read only a boolean phone signal after a user-visible click."""
        from playwright.sync_api import Error as PlaywrightError
        deadline = time.monotonic() + min(5, self.config.timeout_seconds)
        while True:
            self.pause()
            try:
                self.check_access(page)
                evidence = self.extract(page)
                if evidence.get("phone_confirmed") is True:
                    return True
            except PlaywrightError as exc:
                if read_error_kind(exc) not in {"page_changed", "browser_read"}:
                    raise ReadError(READ_ERROR_MESSAGES[read_error_kind(exc)]) from None
            if time.monotonic() >= deadline:
                return False
            self.pause(0.5)

    def evaluate_detail(self, page, evidence: dict, url: str, category: Category):
        if evidence.get("phone_confirmed") is not True:
            candidate, _ = evaluate({**evidence, "phone_confirmed": True},
                                    url, category, self.config)
            if candidate and self.reveal_phone(page):
                evidence["phone_confirmed"] = self.wait_for_revealed_phone(page)
        return evaluate(evidence, url, category, self.config)

    def wait_for_detail_fields(self, page) -> dict:
        """Wait for asynchronous card fields, without waiting for a phone reveal."""
        from playwright.sync_api import Error as PlaywrightError
        deadline = time.monotonic() + self.config.timeout_seconds
        latest = None
        last_error = None
        while True:
            self.pause()
            try:
                self.check_access(page)
                latest = self.extract(page)
                last_error = None
                ready = (latest.get("title") and latest.get("price")
                         and latest.get("registration_labels") and latest.get("category_urls")
                         and latest.get("description_checked"))
                if self.config.seller_mode == "private":
                    ready = ready and latest.get("seller_types")
                if ready:
                    return latest
            except PlaywrightError as exc:
                if page.is_closed():
                    raise ReadError("Вкладка закрылась при чтении карточки.") from None
                last_error = read_error_kind(exc)
                if last_error in {"extract_script", "invalid_selector"}:
                    raise ReadError(READ_ERROR_MESSAGES[last_error]) from None
                # Hydration can replace the document while fields are being read.
            if time.monotonic() >= deadline:
                if latest is not None:
                    return latest  # Missing fields will be rejected by evaluate().
                raise ReadError(READ_ERROR_MESSAGES.get(last_error, "Не удалось прочитать поля карточки после ожидания."))
            self.pause(0.5)

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
                    if self.persistent_profile is not None:
                        context = launch_profile(p, self.config, self.persistent_profile)
                        restore_profile_state(context, self.persistent_profile)
                    else:
                        browser = launch_browser(p, self.config)
                        context_options = {"locale": "pt-PT", "viewport": {"width": 1400, "height": 900}}
                        if self.session_state is not None:
                            context_options["storage_state"] = self.session_state
                        context = browser.new_context(**context_options)
                    context.set_default_timeout(self.config.timeout_seconds * 1000)
                    listing_page = context.new_page()
                    if self.login_ready is not None:
                        if self.config.headless:
                            raise ScanError("Для ручного входа нужен видимый браузер.")
                        listing_page = self.navigate(listing_page, "https://www.olx.pt/")
                        self.log("Войдите в OLX вручную в открытом браузере, затем нажмите «Я вошёл — продолжить» в приложении.")
                        self.on_login_required()
                        while not self.login_ready.wait(0.2):
                            self.pause()
                        self.check_access(listing_page)
                        if self.persistent_profile is None:
                            self.on_session_state(context.storage_state())
                        self.on_login_complete()
                        if self.login_only:
                            summary.message = "Сессия браузера получена. Доступность объявлений проверится при запуске парсера."
                            return summary
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
                            loaded_listing = self.navigate(listing_page, next_url, fresh_on_retry=True)
                            if loaded_listing is None:
                                self.log("Категория недоступна; проверьте её URL в config.json.")
                                break
                            listing_page = loaded_listing
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
                                reason = "read_error"
                                detail_page = context.new_page()
                                try:
                                    loaded_detail = self.navigate(detail_page, url, fresh_on_retry=True)
                                    if loaded_detail is None:
                                        reason = "unavailable"
                                    else:
                                        detail_page = loaded_detail
                                    if loaded_detail is not None and not self.wait_for_content(detail_page, self.config.selectors["title"]):
                                        reason = "title"
                                    elif loaded_detail is not None:
                                        evidence = self.wait_for_detail_fields(detail_page)
                                        if canonical_ad_url(detail_page.url) != url:
                                            reason = "url"
                                        else:
                                            result, reason = self.evaluate_detail(detail_page, evidence, url, category)
                                        del evidence
                                except (AccessBlocked, Stopped):
                                    raise
                                except NavigationError as exc:
                                    reason = "navigation_error"
                                    self.log(str(exc))
                                except ReadError as exc:
                                    reason = "read_error"
                                    self.log(str(exc))
                                except ScanError as exc:
                                    self.log(str(exc))
                                except PlaywrightError as exc:
                                    reason = "read_error"
                                    self.log(READ_ERROR_MESSAGES[read_error_kind(exc)])
                                finally:
                                    if not detail_page.is_closed():
                                        detail_page.close()
                                summary.reasons[reason] += 1
                                if result:
                                    summary.accepted += 1
                                    self.on_result(result)
                                    self.log(f"Объявление #{summary.checked} ({key}): подходит.")
                                else:
                                    self.log(f"Объявление #{summary.checked} ({key}): пропуск — {REASONS[reason]}.")
                                self.on_progress(summary.checked, summary.accepted)
                                self.on_decision(summary.checked, summary.accepted, reason)
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
                            if self.persistent_profile is not None:
                                save_profile_state(context, self.persistent_profile)
                            context.close()
                        except (PlaywrightError, OSError):
                            try:
                                context.close()
                            except PlaywrightError:
                                pass
                            if summary.status == "complete":
                                summary.status = "error"
                                summary.message = "Не удалось сохранить сессию браузера для следующего запуска."
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
