"""
Модуль правил: сопоставление текста → категория / счёт / валюта.
Правила хранятся в rules.json рядом с этим файлом.
"""

import json
import os

RULES_FILE = os.path.join(os.path.dirname(__file__), "rules.json")


def _load_rules() -> list:
    if not os.path.exists(RULES_FILE):
        return []
    with open(RULES_FILE, encoding="utf-8") as f:
        return json.load(f).get("patterns", [])


def apply_rules(text: str, record: dict, cache: dict) -> tuple[dict, list[str]]:
    """
    Проверяет текст на совпадение с правилами из rules.json.
    Возвращает (изменённый record, список предупреждений).
    Применяется первое совпавшее правило.
    """
    rules = _load_rules()
    text_lower = text.lower()
    warnings = []

    for rule in rules:
        keywords = [k.lower() for k in rule.get("match", [])]
        if not any(kw in text_lower for kw in keywords):
            continue

        # Категория
        if cat_name := rule.get("category_name"):
            cat_id = next(
                (cid for cid, c in cache["categories"].items()
                 if c.get("name", "").lower() == cat_name.lower()),
                None,
            )
            if cat_id:
                record["budget_object_id"] = int(cat_id)
            else:
                warnings.append(f"⚠️ Категория «{cat_name}» не найдена в справочнике")

        # Валюта
        if currency_code := rule.get("currency_code"):
            cur_id = next(
                (cid for cid, c in cache["currencies"].items()
                 if c.get("code", "").upper() == currency_code.upper()),
                None,
            )
            if cur_id:
                record["currency_id"] = int(cur_id)
            else:
                warnings.append(f"⚠️ Валюта «{currency_code}» не найдена — нужно добавить её в Дребеденьги")

        break  # применяем только первое совпавшее правило

    return record, warnings
