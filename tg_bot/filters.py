from __future__ import annotations

from dataclasses import replace
from datetime import date

from olx_parser.core import Config, ALL_CATEGORIES, SELLER_CHOICES, apply_search_filters


def category_names(config: Config) -> list[str]:
    return [item.name for item in config.categories if item.enabled]


def validate_filters(config: Config, user: dict) -> Config:
    category = ALL_CATEGORIES if user["category"] == "all" else user["category"]
    choice = next((label for label, mode in SELLER_CHOICES.items()
                   if mode == user["seller_mode"]), None)
    result = apply_search_filters(config, user["registered_from"],
                                  user["registered_to"], category, choice or "")
    target = user["target"]
    if type(target) is not int or not 1 <= target <= 10000:
        raise ValueError("Количество объявлений: целое число от 1 до 10000.")
    return replace(result, target=target)


def validate_date(value: str) -> str:
    if len(value) != 10 or value[4] != "-" or value[7] != "-" or not (value[:4] + value[5:7] + value[8:]).isdigit():
        raise ValueError("Введите дату в формате ГГГГ-ММ-ДД.")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError("Такой даты не существует.") from None


def parse_duration(value: str) -> int | None:
    value = value.strip().lower()
    if value == "навсегда":
        return None
    parts = value.split()
    if len(parts) != 2 or not parts[0].isdigit() or not 1 <= int(parts[0]) <= 36500:
        raise ValueError("Введите, например: 7 дней, 12 часов или навсегда.")
    unit = parts[1]
    if unit in {"день", "дня", "дней"}:
        return int(parts[0]) * 86400
    if unit in {"час", "часа", "часов"}:
        return int(parts[0]) * 3600
    raise ValueError("Укажите дни или часы.")
