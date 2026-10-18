from __future__ import annotations

import calendar
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit


ROOT = Path(__file__).resolve().parent.parent
MONTHS = {
    name: number for number, name in enumerate(
        ("janeiro", "fevereiro", "marco", "abril", "maio", "junho",
         "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"), 1
    )
}
ALLOWED_ROOTS = {
    "tecnologia-e-informatica", "telemoveis-e-tablets", "moveis-casa-e-jardim",
    "moda", "desporto-e-lazer", "equipamentos-e-ferramentas", "compra-venda",
    "bebe-e-crianca", "lazer",
}
CONTACT_RE = re.compile(
    r"(?<!\w)(?:\+?351[\s().-]*)?[29](?:[\s().-]*\d){8}(?!\d)|"
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I
)
AD_PATH = re.compile(r"^/d/anuncio/[^/]+-ID([A-Za-z0-9]+)\.html$")


def normalized(value: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", value.casefold())
                   if not unicodedata.combining(c))


def olx_url(value: str, base: str = "https://www.olx.pt/") -> str | None:
    """Allow only public Portuguese OLX pages, never credentials or API routes."""
    try:
        parts = urlsplit(urljoin(base, value))
        if (parts.scheme != "https" or parts.hostname not in {"olx.pt", "www.olx.pt"}
                or parts.username or parts.password or parts.port not in {None, 443}):
            return None
        path = unquote(parts.path)
        if "\\" in path or any(segment in {".", "..", "api"} for segment in path.split("/")):
            return None
        return urlunsplit(("https", "www.olx.pt", parts.path, parts.query, ""))
    except ValueError:
        return None


def canonical_ad_url(value: str, base: str = "https://www.olx.pt/") -> str | None:
    safe = olx_url(value, base)
    if not safe:
        return None
    parts = urlsplit(safe)
    if not AD_PATH.fullmatch(parts.path) or CONTACT_RE.search(unquote(parts.path)):
        return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def advert_id(url: str) -> str:
    match = AD_PATH.fullmatch(urlsplit(url).path)
    if not match:
        raise ValueError("Некорректная ссылка на объявление OLX.pt.")
    return match.group(1)


def registration_interval(value: str) -> tuple[date, date] | None:
    """Parse only account registration; never publication or last-seen dates.

    Month-only information remains an interval; a day is never invented.
    """
    text = normalized(value.strip())
    match = re.fullmatch(
        r"no olx desde\s+(?:(\d{1,2})\s+de\s+)?([a-z]+)\s+de\s+(\d{4})[.]?", text
    )
    if match:
        day, month_name, year_text = match.groups()
        month = MONTHS.get(month_name)
        if not month:
            return None
        year = int(year_text)
        try:
            if day:
                exact = date(year, month, int(day))
                return exact, exact
            return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
        except ValueError:
            return None
    return None


def euro_price(value: str) -> Decimal | None:
    text = value.replace("\xa0", " ").replace("\u202f", " ").strip()
    match = re.fullmatch(
        r"(\d+(?:(?:[. ]\d{3})+)?(?:,\d{1,2})?)\s*(?:€|EUR)(?:\s*Negociável)?",
        text, re.I,
    )
    if not match:
        return None
    try:
        amount = Decimal(match.group(1).replace(".", "").replace(" ", "").replace(",", "."))
        return amount if amount.is_finite() and amount >= 0 else None
    except InvalidOperation:
        return None


@dataclass(frozen=True)
class Category:
    name: str
    url: str
    enabled: bool


@dataclass(frozen=True)
class Config:
    registered_from: date
    registered_to: date
    target: int
    max_pages_per_category: int
    max_ads_per_run: int
    delay_seconds: float
    timeout_seconds: float
    max_retries: int
    browser_channel: str
    headless: bool
    categories: tuple[Category, ...]
    blocked_category_terms: tuple[str, ...]
    excluded_offer_patterns: tuple[str, ...]
    business_patterns: tuple[str, ...]
    selectors: dict[str, str]

    @classmethod
    def load(cls, path: Path = ROOT / "config.json") -> Config:
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict):
                raise ValueError("config.json должен содержать объект.")
            def integer(key: str, minimum: int, maximum: int) -> int:
                value = raw[key]
                if type(value) is not int or not minimum <= value <= maximum:
                    raise ValueError(f"{key}: допустимо целое число {minimum}–{maximum}.")
                return value

            def seconds(key: str, minimum: float, maximum: float) -> float:
                value = raw[key]
                if type(value) not in {int, float} or not math.isfinite(value) or not minimum <= value <= maximum:
                    raise ValueError(f"{key}: допустимо число {minimum}–{maximum}.")
                return float(value)

            start, end = date.fromisoformat(raw["registered_from"]), date.fromisoformat(raw["registered_to"])
            if start > end:
                raise ValueError("Начало периода регистрации позже конца.")
            if type(raw["headless"]) is not bool:
                raise ValueError("headless должен быть true или false.")
            channel = raw["browser_channel"]
            if channel not in {"auto", "chrome", "msedge", "chromium"}:
                raise ValueError("browser_channel: auto, chrome, msedge или chromium.")
            categories = []
            for item in raw["categories"]:
                url = olx_url(item["url"])
                if not url or urlsplit(url).path.strip("/").split("/")[0] not in ALLOWED_ROOTS:
                    raise ValueError("Категория должна вести в раздел товаров OLX.pt.")
                if not isinstance(item["name"], str) or not item["name"].strip():
                    raise ValueError("У категории отсутствует название.")
                if type(item["enabled"]) is not bool:
                    raise ValueError("enabled должен быть true или false.")
                categories.append(Category(item["name"].strip(), url, item["enabled"]))
            if not any(item.enabled for item in categories):
                raise ValueError("Включите хотя бы одну категорию.")
            lists = {}
            for key in ("blocked_category_terms", "excluded_offer_patterns", "business_patterns"):
                values = raw[key]
                if not isinstance(values, list) or not values or not all(isinstance(x, str) and x for x in values):
                    raise ValueError(f"{key}: требуется непустой список строк.")
                if key.endswith("patterns"):
                    for pattern in values:
                        re.compile(pattern)
                lists[key] = tuple(values)
            selectors = raw["selectors"]
            for key in ("listing_links", "next_link", "title", "price", "parameters", "description", "seller", "contact", "breadcrumbs"):
                if not isinstance(selectors.get(key), str) or not selectors[key].strip():
                    raise ValueError(f"Не задан селектор {key}.")
            return cls(start, end, integer("target", 1, 10000),
                       integer("max_pages_per_category", 1, 1000),
                       integer("max_ads_per_run", 1, 100000),
                       seconds("delay_seconds", 2, 3600), seconds("timeout_seconds", 5, 120),
                       integer("max_retries", 0, 5), channel, raw["headless"], tuple(categories),
                       selectors=dict(selectors), **lists)
        except (OSError, json.JSONDecodeError, KeyError, TypeError, AttributeError, re.error) as exc:
            raise ValueError("Не удалось прочитать config.json: проверьте поля и формат файла.") from exc


@dataclass(frozen=True)
class Listing:
    """The only data allowed in UI/export. No seller or contact attributes."""
    title: str
    price_eur: str
    category: str
    url: str


REASONS = {
    "accepted": "подходит",
    "url": "неподтверждённая ссылка OLX.pt",
    "title": "не найден заголовок объявления",
    "price": "нет подтверждённой цены в евро",
    "private": "не подтверждён статус Particular",
    "business": "обнаружены признаки профессионального предложения",
    "registration_unknown": "период регистрации не подтверждён",
    "registration_range": "регистрация вне заданного периода",
    "phone_unknown": "наличие телефона не подтверждено",
    "category": "категория не подтверждена или исключена",
    "offer": "не подтверждена продажа физического товара",
    "unavailable": "объявление недоступно",
    "page_error": "ошибка загрузки страницы",
}


def evaluate(evidence: dict, url: str, category: Category, config: Config,
             today: date | None = None) -> tuple[Listing | None, str]:
    """Use transient evidence. Return only the approved product or a fixed reason."""
    url = canonical_ad_url(url)
    if not url:
        return None, "url"
    title = evidence.get("title", "").strip()
    if not title:
        return None, "title"
    price = euro_price(evidence.get("price", ""))
    if price is None:
        return None, "price"
    types = {normalized(x.strip()) for x in evidence.get("seller_types", [])}
    if evidence.get("business_signal") is not False or types & {"profissional", "empresa", "negocio"}:
        return None, "business"
    if types != {"particular"}:
        return None, "private"
    intervals = {registration_interval(text) for text in evidence.get("registration_labels", [])}
    if not intervals or None in intervals or len(intervals) != 1:
        return None, "registration_unknown"
    first, last = intervals.pop()
    today = today or date.today()
    if first < config.registered_from or last > config.registered_to or first > today:
        return None, "registration_range"
    if evidence.get("phone_confirmed") is not True:
        return None, "phone_unknown"
    breadcrumbs = [olx_url(x) for x in evidence.get("category_urls", [])]
    paths = [normalized(unquote(urlsplit(x).path)) for x in breadcrumbs if x]
    prefix = urlsplit(category.url).path.rstrip("/") + "/"
    if not any(path.rstrip("/") + "/" == prefix for path in paths):
        return None, "category"
    for path in paths:
        segments = path.strip("/").split("/")
        if any(term in segments or any(segment.startswith(term + "-") for segment in segments)
               for term in config.blocked_category_terms):
            return None, "category"
    if evidence.get("description_checked") is not True or evidence.get("excluded_offer") is not False:
        return None, "offer"
    clean_title = CONTACT_RE.sub("[контакт скрыт]", title)
    return Listing(clean_title, format(price, ".2f"), category.name, url), "accepted"
