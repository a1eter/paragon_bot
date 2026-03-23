"""
Telegram-бот для добавления записей в Drebedengi.

Формат ввода (примеры):
  кофе 150
  зарплата 50000 доход
  такси 350 #транспорт
  обед 450 #еда кафе рядом с работой
"""

import logging
import os
import re
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from drebedengi_api import DrebedengiClient
from rules import apply_rules

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(os.path.dirname(__file__), "paragon_bot.log"),
            encoding="utf-8",
        ),
    ],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Конфиг
# ──────────────────────────────────────────────────────────────

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_IDS = set(
    int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()
)

DD_API_ID = os.getenv("DD_API_ID", "demo_api")
DD_LOGIN = os.getenv("DD_LOGIN", "demo@example.com")
DD_PASS = os.getenv("DD_PASS", "demo")

dd = DrebedengiClient(DD_API_ID, DD_LOGIN, DD_PASS)

# Простой кэш справочников (загружается один раз при старте /refresh)
_cache: dict = {}


def get_cache() -> dict:
    return _cache


def load_cache() -> None:
    global _cache
    currencies = {c["id"]: c for c in dd.get_currency_list()}
    categories = {c["id"]: c for c in dd.get_category_list()}
    places = {p["id"]: p for p in dd.get_place_list()}
    sources = {s["id"]: s for s in dd.get_source_list()}

    # Найти дефолтную валюту
    _dd_cur_code = os.getenv("DD_DEFAULT_CURRENCY", "").strip().upper()
    if _dd_cur_code:
        default_currency_id = next(
            (cid for cid, c in currencies.items() if c.get("code", "").upper() == _dd_cur_code),
            next(
                (cid for cid, c in currencies.items() if c.get("is_default") == "1"),
                next(iter(currencies)) if currencies else None,
            ),
        )
    else:
        default_currency_id = next(
            (cid for cid, c in currencies.items() if c.get("is_default") == "1"),
            next(iter(currencies)) if currencies else None,
        )
    # Первый видимый счёт
    default_place_id = next(
        (pid for pid, p in places.items() if p.get("is_hidden") != "1"),
        next(iter(places)) if places else None,
    )
    # Первая видимая категория расходов (type=3)
    default_category_id = next(
        (cid for cid, c in categories.items()
         if c.get("is_hidden") != "1" and c.get("type") == "3"),
        next(iter(categories)) if categories else None,
    )

    _cache = {
        "currencies": currencies,
        "categories": categories,
        "places": places,
        "sources": sources,
        "default_currency_id": default_currency_id,
        "default_place_id": default_place_id,
        "default_category_id": default_category_id,
    }
    logger.info(
        "Кэш загружен: %d валют, %d категорий, %d счетов",
        len(currencies), len(categories), len(places),
    )


# ──────────────────────────────────────────────────────────────
# Парсинг текстового ввода (локальный fallback)
# ──────────────────────────────────────────────────────────────

def parse_entry_local(text: str) -> dict | None:
    """
    Упрощённый парсер:
      [дата] [комментарий] [сумма] [доход|расход]
    Дата — DD.MM.YYYY, DD.MM.YY, YYYY-MM-DD — в начале или конце строки.
    Сумма — число (целое или с запятой/точкой), может быть с 'р'/'руб'.
    """
    text = text.strip()

    # Извлекаем дату если есть
    operation_date = None
    date_re = re.compile(
        r"(?:^|\s)(\d{2}\.\d{2}\.\d{4}|\d{2}\.\d{2}\.\d{2}|\d{4}-\d{2}-\d{2})(?:\s|$)"
    )
    dm = date_re.search(text)
    if dm:
        raw_date = dm.group(1)
        try:
            if "-" in raw_date:
                operation_date = datetime.strptime(raw_date, "%Y-%m-%d").strftime("%Y-%m-%d %H:%M:%S")
            elif len(raw_date) == 8:  # DD.MM.YY
                operation_date = datetime.strptime(raw_date, "%d.%m.%y").strftime("%Y-%m-%d %H:%M:%S")
            else:
                operation_date = datetime.strptime(raw_date, "%d.%m.%Y").strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
        text = (text[: dm.start(1)] + text[dm.end(1):]).strip()
    # Ищем число (сумму): сначала десятичное (3.79 / 3,79), потом целое
    m = re.search(r"(\d+[.,]\d+|\d+)[\s]*(р|руб|рублей|[$€zł])?", text, re.IGNORECASE)
    if not m:
        return None

    amount_str = m.group(1).replace(",", ".")
    try:
        amount = float(amount_str)
    except ValueError:
        return None

    comment = (text[: m.start()] + " " + text[m.end():]).strip()
    operation_type = 3  # расход по умолчанию

    # Ключевые слова типа операции
    if re.search(r"\bдоход\b|зарплата|зп\b|получил", comment, re.IGNORECASE):
        operation_type = 2

    # Очищаем ключевые слова из комментария
    comment = re.sub(r"\bдоход\b|зарплата|зп\b|получил", "", comment, flags=re.IGNORECASE)
    comment = re.sub(r"\s{2,}", " ", comment).strip()

    return {
        "amount": amount,
        "comment": comment or "без комментария",
        "operation_type": operation_type,
        "operation_date": operation_date,
    }


# ──────────────────────────────────────────────────────────────
# Middleware: проверка пользователя
# ──────────────────────────────────────────────────────────────

def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True  # Если список пустой — разрешаем всем (только для теста!)
    uid = update.effective_user.id if update.effective_user else None
    return uid in ALLOWED_USER_IDS


# ──────────────────────────────────────────────────────────────
# Обработчики команд
# ──────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else "?"
    await update.message.reply_text(
        f"Telegram ID: <code>{uid}</code>",
        parse_mode="HTML",
    )


# ──────────────────────────────────────────────────────────────
# Помощники для классификации
# ──────────────────────────────────────────────────────────────

# Избранные счета для быстрого выбора (id → метка кнопки)
FAVORITE_PLACES = [
    ("16318720", "Revolut"),
    ("15310091", "PKO"),
    ("16299773", "Santander A"),
    ("16395412", "Santander M"),
    ("16726444", "Bybit"),
    ("10407734", "Мой кошелёк"),
]

# Избранные категории для быстрого выбора
FAVORITE_CAT_IDS = [
    "10407590",  # Продукты питания и напитки
    "10407651",  # Бытовая химия и инвентарь
    "10407642",  # Кухонные принадлежности
    "10407620",  # Косметика, средства гигиены
    "10407616",  # Одежда, обувь
    "10407604",  # Спиртные напитки и закуски
    "13070223",  # Текстиль и товары для дома
    "10407623",  # Подарки, открытки
    "14377692",  # Детская гигиена
    "14427293",  # Детская комната
]


def _is_default_category(record: dict, cache: dict) -> bool:
    """True если запись попала в категорию по умолчанию (не была распознана)."""
    return str(record.get("budget_object_id")) == str(cache.get("default_category_id"))


def _save_rule_keyword(keyword: str, cat_id, cache: dict) -> None:
    """Добавляет или обновляет правило keyword → категория в rules.json."""
    import json as _json
    rules_file = os.path.join(os.path.dirname(__file__), "rules.json")
    if os.path.exists(rules_file):
        with open(rules_file, encoding="utf-8") as f:
            data = _json.load(f)
    else:
        data = {"patterns": []}

    cat = cache["categories"].get(str(cat_id)) or cache["categories"].get(cat_id)
    cat_name = cat.get("name") if cat else str(cat_id)
    kw = keyword.lower().strip()

    # Обновляем существующее правило с тем же ключевым словом
    for rule in data["patterns"]:
        if kw in [m.lower() for m in rule.get("match", [])]:
            rule["category_name"] = cat_name
            break
    else:
        for rule in data["patterns"]:
            if rule.get("category_name", "").lower() == cat_name.lower():
                rule["match"].append(kw)
                break
        else:
            data["patterns"].append({"match": [kw], "category_name": cat_name})

    with open(rules_file, "w", encoding="utf-8") as f:
        _json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("Правило сохранено: %s → %s", kw, cat_name)


def _account_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора счёта."""
    buttons = []
    row = []
    for place_id, label in FAVORITE_PLACES:
        row.append(InlineKeyboardButton(label, callback_data=f"acc_sel_{place_id}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("🔍 Искать другой…", callback_data="acc_search")])
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def _account_search_keyboard(results: list) -> InlineKeyboardMarkup:
    """Клавиатура с результатами поиска счёта."""
    buttons = [[InlineKeyboardButton(name, callback_data=f"acc_sel_{pid}")] for pid, name in results[:15]]
    buttons.append([InlineKeyboardButton("◀️ К избранным", callback_data="acc_back")])
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(buttons)


def _category_keyboard(cache: dict) -> InlineKeyboardMarkup:
    """Клавиатура с избранными категориями + кнопка поиска."""
    buttons = []
    row = []
    for cid in FAVORITE_CAT_IDS:
        cat = cache["categories"].get(cid)
        if not cat:
            continue
        row.append(InlineKeyboardButton(cat["name"], callback_data=f"cat_sel_{cid}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("🔍 Искать другую…", callback_data="cat_search")])
    buttons.append([InlineKeyboardButton("⏭ Пропустить (Без категории)", callback_data="cat_skip")])
    return InlineKeyboardMarkup(buttons)


def _category_search_keyboard(results: list) -> InlineKeyboardMarkup:
    """Клавиатура с результатами поиска."""
    buttons = [[InlineKeyboardButton(name, callback_data=f"cat_sel_{cid}")] for cid, name in results[:15]]
    buttons.append([InlineKeyboardButton("◀️ К избранным", callback_data="cat_back")])
    buttons.append([InlineKeyboardButton("⏭ Пропустить (Без категории)", callback_data="cat_skip")])
    return InlineKeyboardMarkup(buttons)


def _classify_prompt(item: dict, remaining: int) -> str:
    more = f" (ещё {remaining - 1} после)" if remaining > 1 else ""
    return (
        f"❓ Не знаю категорию для: <b>{item['line']}</b>{more}\n"
        f"Сумма: {item['amount']:.2f} {item['cur_code']}\n\n"
        f"Выбери категорию — запомню на будущее:"
    )


def _build_preview(records: list, cache: dict, warnings: list) -> str:
    if len(records) == 1:
        record = records[0]
        op_label = {2: "📈 Доход", 3: "📉 Расход", 4: "↔️ Перевод"}.get(record["operation_type"], "Запись")
        amount_display = record["sum"] / 100
        place = cache["places"].get(str(record["place_id"])) or cache["places"].get(record["place_id"])
        place_name = place.get("name") if place else str(record["place_id"])
        obj_id = str(record["budget_object_id"])
        cat = cache["categories"].get(obj_id) or cache["sources"].get(obj_id)
        cat_name = cat.get("name") if cat else obj_id
        cur = cache["currencies"].get(str(record["currency_id"])) or cache["currencies"].get(record["currency_id"])
        cur_code = cur.get("code", "?") if cur else "?"
        orig_str = f"{record['original_sum'] / 100:.2f}\u2192" if "original_sum" in record else ""
        text = (
            f"<b>{op_label}</b>\n"
            f"Сумма: <b>{orig_str}{amount_display:.2f} {cur_code}</b>\n"
            f"Счёт: {place_name}\n"
            f"Категория: {cat_name}\n"
            f"Комментарий: {record['comment']}\n"
            f"Дата: {record['operation_date'][:16]}"
        )
    else:
        lines_preview = []
        totals: dict[tuple, float] = {}
        for i, record in enumerate(records, 1):
            op = {2: "📈", 3: "📉", 4: "↔️"}.get(record["operation_type"], "•")
            amount_display = record["sum"] / 100
            cur = cache["currencies"].get(str(record["currency_id"])) or cache["currencies"].get(record["currency_id"])
            cur_code = cur.get("code", "?") if cur else "?"
            obj_id = str(record["budget_object_id"])
            cat = cache["categories"].get(obj_id) or cache["sources"].get(obj_id)
            cat_name = cat.get("name") if cat else "?"
            orig_str = f"{record['original_sum'] / 100:.2f}\u2192" if "original_sum" in record else ""
            lines_preview.append(f"{i}. {op} <b>{orig_str}{amount_display:.2f} {cur_code}</b> — {record['comment']} [{cat_name}]")
            key = (record["operation_type"], cur_code)
            totals[key] = totals.get(key, 0.0) + amount_display
        totals_str = ", ".join(f"{v:.2f} {k[1]}" for k, v in totals.items())
        text = f"<b>📋 Группа записей ({len(records)}):</b>\n" + "\n".join(lines_preview)
        text += f"\n\n<b>Итого: {totals_str}</b>"
    if warnings:
        text += "\n\n" + "\n".join(warnings)
    return text


_CONFIRM_KB = InlineKeyboardMarkup([[
    InlineKeyboardButton("✅ Записать", callback_data="confirm"),
    InlineKeyboardButton("❌ Отмена", callback_data="cancel"),
]])

# Словарь эмодзи по ключевым словам в названии категории
_CAT_EMOJI_MAP = [
    (["продукты", "питание"], "🛒"),
    (["спиртные", "бары", "клубы"], "🍷"),
    (["кафе", "ресторан", "доставка", "обед"], "🍽"),
    (["химия", "инвентарь", "хозяйственные"], "🧹"),
    (["косметика", "гигиена", "салон", "парикмахер"], "🧴"),
    (["одежда", "обувь"], "👗"),
    (["детская", "детские", "детское", "детский", "ясли", "садик"], "🧸"),
    (["кухонные"], "🍳"),
    (["текстиль", "мебель", "интерьер", "жильё", "аренда жилья", "коммунальные", "уборка"], "🏠"),
    (["подарки", "сувениры", "цветы"], "🎁"),
    (["такси"], "🚕"),
    (["транспорт", "общественный транспорт", "проездные", "топливо", "бензин", "парковка", "мойка", "автомобиль", "авто"], "🚗"),
    (["медицина", "лекарственные", "врачей", "здравоохранение", "страхование здоровья"], "💊"),
    (["фитнес", "спорт", "бег", "теннис", "бильярд", "боулинг", "сауна", "баня", "бассейн", "массаж"], "💪"),
    (["обучение", "образование", "курсы", "книги", "журналы", "высшее"], "📚"),
    (["техника", "электроника", "инструмент"], "💻"),
    (["soft", "программы", "игры", "подписки", "интернет", "телефон", "домен"], "📱"),
    (["налог", "пошлин", "сборы", "штраф"], "📋"),
    (["страхование"], "🛡"),
    (["покер", "казино", "игровые", "ставки", "tournaments", "ring games"], "🃏"),
    (["kaucja"], "📦"),
    (["без категории"], "📂"),
    (["крипта", "fees", "комиссии"], "💰"),
    (["отдых", "развлечения", "кино", "театр", "концерт", "билеты", "хобби", "фото"], "🎭"),
    (["coaching", "курсы"], "🎓"),
    (["отели", "гостиницы", "кемпинги"], "🏨"),
    (["корректировка"], "🔧"),
]

def _cat_emoji(cat_name: str) -> str:
    low = cat_name.lower()
    for keywords, emoji in _CAT_EMOJI_MAP:
        if any(kw in low for kw in keywords):
            return emoji
    return "🏷"


def _build_success_message(records: list, cache: dict) -> str:
    """Формирует итоговое сообщение после успешной записи: по категориям."""
    from collections import defaultdict

    # Дата и время из первой записи
    date_raw = records[0].get("operation_date", "")
    try:
        dt = datetime.strptime(date_raw[:16], "%Y-%m-%d %H:%M")
        date_str = dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        date_str = date_raw[:16]

    # Счёт из первой записи
    place = cache["places"].get(str(records[0]["place_id"])) or cache["places"].get(records[0]["place_id"])
    place_name = place.get("name", "?") if place else "?"

    # Группируем по категории
    groups: dict = defaultdict(list)
    total = 0.0
    cur_code = "?"
    for record in records:
        obj_id = str(record["budget_object_id"])
        cat = cache["categories"].get(obj_id) or cache["sources"].get(obj_id)
        cat_name = cat.get("name", obj_id) if cat else obj_id
        cur = cache["currencies"].get(str(record["currency_id"])) or cache["currencies"].get(record["currency_id"])
        cur_code = cur.get("code", "?") if cur else "?"
        amount = record["sum"] / 100
        # Убираем тег [LIDL] из комментария для отображения
        comment = record["comment"]
        if comment.startswith("[LIDL] "):
            comment = comment[7:]
        groups[cat_name].append((comment, amount, cur_code))
        total += amount

    lines = [f"📅 {date_str}\n"]
    for cat_name, items in groups.items():
        emoji = _cat_emoji(cat_name)
        lines.append(f"{emoji} <b>{cat_name}</b>")
        for comment, amount, code in items:
            lines.append(f"  {comment}  {amount:.2f} {code}")
        lines.append("")

    lines.append("─" * 21)
    lines.append(f"<b>Итого: {total:.2f} {cur_code}</b>")
    lines.append(f"💳 {place_name}")

    return "\n".join(lines)



def _lidl_to_records(parsed: dict, cache: dict, base_client_id: int) -> tuple:
    """
    Конвертирует распознанный чек Лидл в список записей.
    Возвращает (records, valid_lines, warnings).
    valid_lines[i] = оригинальное название позиции для classify_queue.
    """
    # Инвертируем: первый товар в чеке → наибольший client_id → первый в дребеденьгах
    items = list(reversed(parsed["items"]))
    date_str = parsed.get("date")
    time_str = parsed.get("time") or "00:00:00"
    if time_str.count(":") == 1:
        time_str += ":00"
    op_date = (
        f"{date_str} {time_str}"
        if date_str
        else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )

    n = len(items)
    group_id = str(base_client_id) if n > 1 else None

    records: list = []
    valid_lines: list = []
    warnings = list(parsed.get("warnings", []))

    for i, item in enumerate(items):
        amount_kopecks = round(item["adjusted_price"] * 100)
        if amount_kopecks <= 0:
            warnings.append(f"⚠️ Пропущена позиция \u00ab{item['name']}\u00bb: сумма {item['adjusted_price']:.2f}")
            continue

        record = {
            "client_id":        base_client_id + i,
            "place_id":         int(cache["default_place_id"]),
            "budget_object_id": int(cache["default_category_id"]),
            "sum":              amount_kopecks,
            "operation_date":   op_date,
            "comment":          item["name"],
            "currency_id":      int(cache["default_currency_id"]),
            "is_duty":          False,
            "operation_type":   3,
        }

        # Сохраняем оригинальную цену для превью, если zwrot изменил цену
        orig_kopecks = round(item["price"] * 100)
        if orig_kopecks != amount_kopecks:
            record["original_sum"] = orig_kopecks

        if group_id:
            record["group_id"] = group_id

        # Применяем правила по имени позиции
        record, rule_warnings = apply_rules(item["name"], record, cache)
        warnings.extend(rule_warnings)

        # Тег [LIDL]
        if not record["comment"].startswith("[LIDL]"):
            record["comment"] = "[LIDL] " + record["comment"]

        records.append(record)
        valid_lines.append(item["name"])

    return records, valid_lines, warnings


# ──────────────────────────────────────────────────────────────
# Основной обработчик: добавление записи
# ──────────────────────────────────────────────────────────────

def _parse_line(text: str, cache: dict) -> tuple[dict | None, list[str]]:
    """Парсит одну строку в запись. Возвращает (record, warnings) или (None, [])."""
    # Сначала пробуем серверный парсер
    try:
        parsed_list = dd.parse_text_data([text])
        parsed = parsed_list[0] if parsed_list else None
    except Exception:
        parsed = None

    if not parsed or not parsed.get("sum") or parsed.get("sum") == "0":
        parsed_local = parse_entry_local(text)
        if not parsed_local:
            return None, []
        amount_kopecks = int(parsed_local["amount"] * 100)
        operation_type = parsed_local["operation_type"]
        comment = parsed_local["comment"]
        default_obj_id = (
            cache["default_category_id"]
            if operation_type == 3
            else next(iter(cache["sources"]), None)
        )
        record = {
            "client_id": 1,
            "place_id": int(cache["default_place_id"]),
            "budget_object_id": int(default_obj_id),
            "sum": amount_kopecks,
            "operation_date": parsed_local["operation_date"] or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "comment": comment,
            "currency_id": int(cache["default_currency_id"]),
            "is_duty": False,
            "operation_type": operation_type,
        }
    else:
        raw_sum = int(parsed.get("sum", 0))
        record = {
            "client_id": 1,
            "place_id": int(parsed.get("place_from_id") or cache["default_place_id"]),
            "budget_object_id": int(parsed.get("cat_id") or cache["default_category_id"]),
            "sum": abs(raw_sum),
            "operation_date": parsed.get("date") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "comment": parsed.get("comment", text),
            "currency_id": int(parsed.get("cur") or cache["default_currency_id"]),
            "is_duty": False,
            "operation_type": int(parsed.get("type", 3)),
        }

    # Если в тексте нет явного символа валюты — применяем дефолтную
    if not re.search(r'[р$€£₽zł]|руб', text, re.IGNORECASE):
        record["currency_id"] = int(cache["default_currency_id"])

    record, warnings = apply_rules(text, record, cache)

    # Добавляем тег [LIDL] в начало комментария
    if not record["comment"].startswith("[LIDL]"):
        record["comment"] = "[LIDL] " + record["comment"]

    return record, warnings


async def handle_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return

    text = update.message.text.strip()

    # ── Режим поиска счёта ────────────────────────────────────
    if context.user_data.get("acc_search_active"):
        context.user_data.pop("acc_search_active", None)
        cache = get_cache()
        search_term = text.lower()
        place_list = [
            (pid, p["name"])
            for pid, p in cache["places"].items()
            if search_term in p["name"].lower()
        ]
        chat_id = context.user_data.pop("acc_search_chat_id", None)
        msg_id  = context.user_data.pop("acc_search_msg_id", None)
        try:
            await update.message.delete()
        except Exception:
            pass
        if not place_list:
            kb   = _account_keyboard()
            note = f"\n\n❌ По запросу «{text}» ничего не найдено. Попробуй снова:"
        else:
            kb   = _account_search_keyboard(place_list)
            note = f"\n\n🔍 Результаты для «{text}»:"
        await context.bot.edit_message_text(
            "💳 <b>С какого счёта?</b>" + note,
            chat_id=chat_id,
            message_id=msg_id,
            parse_mode="HTML",
            reply_markup=kb,
        )
        return

    # ── Режим поиска категории ────────────────────────────────
    if context.user_data.get("cat_search_active"):
        context.user_data.pop("cat_search_active", None)
        cache = get_cache()
        search_term = text.lower()
        cat_list = [
            (cid, c["name"])
            for cid, c in cache["categories"].items()
            if c.get("type") == "3" and search_term in c["name"].lower()
        ]
        classify_queue = context.user_data.get("classify_queue", [])
        chat_id = context.user_data.pop("cat_classify_chat_id", None)
        msg_id = context.user_data.pop("cat_classify_msg_id", None)
        current = classify_queue[0] if classify_queue else None
        prompt = _classify_prompt(current, len(classify_queue)) if current else "Выбери категорию:"
        try:
            await update.message.delete()
        except Exception:
            pass
        if not cat_list:
            kb = _category_keyboard(cache)
            note = f"\n\n❌ По запросу «{text}» ничего не найдено. Попробуй снова:"
        else:
            kb = _category_search_keyboard(cat_list)
            note = f"\n\n🔍 Результаты для «{text}»:"
        await context.bot.edit_message_text(
            prompt + note,
            chat_id=chat_id,
            message_id=msg_id,
            parse_mode="HTML",
            reply_markup=kb,
        )
        return

    cache = get_cache()

    if not cache:
        await update.message.reply_text("Сначала загрузи справочники: /refresh")
        return

    # ── Чек Лидл (Польша) ────────────────────────────────────────
    # Старый формат: итог в строке "Razem", нет "Suma PLN"
    # Новый формат: есть строка "Suma PLN"
    _LIDL_ITEM_RE = re.compile(
        r'(?:\d[\d,.]*\s*\*\s*\d[\d,.]+|\d[\d,.]*\s*kg\s*x\s*\d[\d,.]+)'
        r'\s+[\d,.]+\s+[A-Z]\s*$',
        re.M,
    )
    if "Suma PLN" in text or _LIDL_ITEM_RE.search(text):
        from lidl_parser import parse_lidl_receipt
        parsed_receipt = parse_lidl_receipt(text)
        if not parsed_receipt["items"]:
            await update.message.reply_text("Не удалось распознать позиции в чеке.")
            return
        base_client_id = int(datetime.now().timestamp())
        records, valid_lines, all_warnings = _lidl_to_records(parsed_receipt, cache, base_client_id)
        if not records:
            await update.message.reply_text("Не удалось преобразовать чек в записи.")
            return
        classify_queue = []
        for i, (record, line) in enumerate(zip(records, valid_lines)):
            if _is_default_category(record, cache):
                cur = cache["currencies"].get(str(record["currency_id"])) or cache["currencies"].get(record["currency_id"])
                cur_code = cur.get("code", "?") if cur else "?"
                comment_words = [w for w in record["comment"].split() if not (w.startswith("[") and w.endswith("]"))]
                keyword = comment_words[0] if comment_words else line.split()[0]
                classify_queue.append({
                    "idx":      i,
                    "keyword":  keyword,
                    "line":     line,
                    "amount":   record["sum"] / 100,
                    "cur_code": cur_code,
                })
        context.user_data["pending_records"] = records
        context.user_data["pending_warnings"] = all_warnings
        if classify_queue:
            context.user_data["classify_queue"] = classify_queue
        n = len(records)
        total = sum(r["sum"] / 100 for r in records)
        cur = cache["currencies"].get(str(records[0]["currency_id"])) or cache["currencies"].get(records[0]["currency_id"])
        cur_code = cur.get("code", "?") if cur else "?"
        summary = f"{n} {'запись' if n == 1 else 'записи' if 2 <= n <= 4 else 'записей'}, итого {total:.2f} {cur_code}"
        date_line = f"\n📅 {parsed_receipt['date']}" if parsed_receipt.get('date') else ""
        await update.message.reply_text(
            f"🧾 {summary}{date_line}\n\n💳 <b>С какого счёта?</b>",
            parse_mode="HTML",
            reply_markup=_account_keyboard(),
        )
        return

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    lines = list(reversed(lines))

    # ── Извлекаем дату и время из отдельных строк ─────────────
    # Форматы даты: 2026-03-18 или 18.03.2026 или 18.03.26
    _date_only_re = re.compile(r"^(\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{4}|\d{2}\.\d{2}\.\d{2})$")
    # Форматы времени: 12:45 или 12:45:00
    _time_only_re = re.compile(r"^(\d{1,2}:\d{2}(?::\d{2})?)$")

    group_date = None  # только дата
    group_time = "00:00:00"
    product_lines = []
    for line in lines:
        dm = _date_only_re.match(line)
        tm = _time_only_re.match(line)
        if dm:
            raw = dm.group(1)
            try:
                if "-" in raw:
                    group_date = datetime.strptime(raw, "%Y-%m-%d")
                elif len(raw) == 8:
                    group_date = datetime.strptime(raw, "%d.%m.%y")
                else:
                    group_date = datetime.strptime(raw, "%d.%m.%Y")
            except ValueError:
                product_lines.append(line)
        elif tm:
            raw_t = tm.group(1)
            group_time = raw_t if raw_t.count(":") == 2 else raw_t + ":00"
        else:
            product_lines.append(line)

    group_operation_date = None
    if group_date:
        group_operation_date = group_date.strftime(f"%Y-%m-%d {group_time}")

    lines = product_lines

    records = []
    valid_lines = []
    all_warnings = []
    failed_lines = []
    base_client_id = int(datetime.now().timestamp())
    is_group = len(lines) > 1
    group_id = str(base_client_id) if is_group else None

    for i, line in enumerate(lines):
        record, warnings = _parse_line(line, cache)
        if record is None:
            failed_lines.append(line)
        else:
            record["client_id"] = base_client_id + i
            if group_id:
                record["group_id"] = group_id
            if group_operation_date:
                record["operation_date"] = group_operation_date
            records.append(record)
            valid_lines.append(line)
            all_warnings.extend(warnings)

    if not records:
        await update.message.reply_text(
            "Не могу распознать запись. Попробуй:\n"
            "  <b>кофе 150</b>\n"
            "  <b>зарплата 50000 доход</b>",
            parse_mode="HTML",
        )
        return

    # Очередь записей с неизвестной категорией
    classify_queue = []
    for i, (record, line) in enumerate(zip(records, valid_lines)):
        if _is_default_category(record, cache):
            cur = cache["currencies"].get(str(record["currency_id"])) or cache["currencies"].get(record["currency_id"])
            cur_code = cur.get("code", "?") if cur else "?"
            # Берём первое слово комментария, пропуская тег [LIDL]
            comment_words = [w for w in record["comment"].split() if not (w.startswith("[") and w.endswith("]"))]
            keyword = comment_words[0] if comment_words else line.split()[0]
            classify_queue.append({
                "idx": i,
                "keyword": keyword,
                "line": line,
                "amount": record["sum"] / 100,
                "cur_code": cur_code,
            })

    context.user_data["pending_records"] = records
    context.user_data["pending_warnings"] = all_warnings
    if classify_queue:
        context.user_data["classify_queue"] = classify_queue

    n = len(records)
    total = sum(r["sum"] / 100 for r in records)
    cur = cache["currencies"].get(str(records[0]["currency_id"])) or cache["currencies"].get(records[0]["currency_id"])
    cur_code = cur.get("code", "?") if cur else "?"
    summary = f"{n} {'запись' if n == 1 else 'записи' if 2 <= n <= 4 else 'записей'}, итого {total:.2f} {cur_code}"
    date_line = f"\n📅 {group_operation_date[:10]}" if group_operation_date else ""
    await update.message.reply_text(
        f"📋 {summary}{date_line}\n\n💳 <b>С какого счёта?</b>",
        parse_mode="HTML",
        reply_markup=_account_keyboard(),
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    # ── Поиск счёта ───────────────────────────────────────────
    if query.data == "acc_search":
        context.user_data["acc_search_active"] = True
        context.user_data["acc_search_chat_id"] = query.message.chat_id
        context.user_data["acc_search_msg_id"]  = query.message.message_id
        await query.edit_message_text(
            "💳 <b>С какого счёта?</b>\n\n🔍 <i>Напиши часть названия счёта:</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data="acc_back")]]),
        )
        return

    if query.data == "acc_back":
        context.user_data.pop("acc_search_active", None)
        await query.edit_message_text(
            "💳 <b>С какого счёта?</b>",
            parse_mode="HTML",
            reply_markup=_account_keyboard(),
        )
        return

    # ── Выбор счёта ───────────────────────────────────────────
    if query.data.startswith("acc_sel_"):
        place_id = int(query.data[8:])
        records = context.user_data.get("pending_records", [])
        if not records:
            await query.edit_message_text("Запись устарела, попробуй снова.")
            return
        for r in records:
            r["place_id"] = place_id
        cache = get_cache()
        classify_queue = context.user_data.get("classify_queue", [])
        if classify_queue:
            first = classify_queue[0]
            await query.edit_message_text(
                _classify_prompt(first, len(classify_queue)),
                parse_mode="HTML",
                reply_markup=_category_keyboard(cache),
            )
        else:
            warnings = context.user_data.get("pending_warnings", [])
            preview = _build_preview(records, cache, warnings)
            await query.edit_message_text(preview, parse_mode="HTML", reply_markup=_CONFIRM_KB)
        return

    # ── Классификация категорий ───────────────────────────────
    if query.data.startswith("cat_"):
        classify_queue = context.user_data.get("classify_queue", [])
        if not classify_queue:
            await query.edit_message_text("Классификация устарела, попробуй снова.")
            return

        current = classify_queue[0]
        cache = get_cache()

        if query.data == "cat_noop":
            return

        if query.data == "cat_search":
            context.user_data["cat_search_active"] = True
            context.user_data["cat_classify_chat_id"] = query.message.chat_id
            context.user_data["cat_classify_msg_id"] = query.message.message_id
            await query.edit_message_text(
                query.message.text + "\n\n🔍 <i>Напиши часть названия категории:</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data="cat_back")]]),
            )
            return

        if query.data == "cat_back":
            context.user_data.pop("cat_search_active", None)
            await query.edit_message_text(
                _classify_prompt(current, len(classify_queue)),
                parse_mode="HTML",
                reply_markup=_category_keyboard(cache),
            )
            return

        if query.data.startswith("cat_sel_"):
            cat_id = query.data[8:]
            records = context.user_data.get("pending_records", [])
            idx = current["idx"]
            if idx < len(records):
                records[idx]["budget_object_id"] = int(cat_id)
            _save_rule_keyword(current["keyword"], cat_id, cache)
            classify_queue.pop(0)
            context.user_data["classify_queue"] = classify_queue

        elif query.data == "cat_skip":
            records = context.user_data.get("pending_records", [])
            idx = current["idx"]
            if idx < len(records):
                records[idx]["budget_object_id"] = 10407760
            classify_queue.pop(0)
            context.user_data["classify_queue"] = classify_queue

        if classify_queue:
            next_item = classify_queue[0]
            await query.edit_message_text(
                _classify_prompt(next_item, len(classify_queue)),
                parse_mode="HTML",
                reply_markup=_category_keyboard(cache),
            )
        else:
            records = context.user_data.get("pending_records", [])
            warnings = context.user_data.get("pending_warnings", [])
            preview = _build_preview(records, cache, warnings)
            await query.edit_message_text(preview, parse_mode="HTML", reply_markup=_CONFIRM_KB)
        return

    # ── Подтверждение / отмена ────────────────────────────────
    if query.data == "confirm":
        records = context.user_data.pop("pending_records", None)
        context.user_data.pop("pending_warnings", None)
        context.user_data.pop("classify_queue", None)
        if not records:
            await query.edit_message_text("Запись устарела, попробуй снова.")
            return
        try:
            result, raw_response = dd.set_record_list(records)
            if isinstance(result, list) and result:
                msg = _build_success_message(records, cache)
                await query.edit_message_text(msg, parse_mode="HTML")
            elif result is None:
                logger.warning("Ответ сервера не распознан. raw=\n%s", raw_response)
                await query.edit_message_text(
                    f"❓ Сервер не подтвердил запись. Ответ:\n<code>{raw_response[:600]}</code>",
                    parse_mode="HTML",
                )
            else:
                msg = _build_success_message(records, cache)
                await query.edit_message_text(msg, parse_mode="HTML")
        except Exception as e:
            await query.edit_message_text(f"❌ Ошибка при записи: {e}")

    elif query.data == "cancel":
        context.user_data.pop("pending_records", None)
        context.user_data.pop("pending_warnings", None)
        context.user_data.pop("classify_queue", None)
        await query.edit_message_text("Отменено.")


# ──────────────────────────────────────────────────────────────
# Запуск
# ──────────────────────────────────────────────────────────────

def main() -> None:
    # Загружаем справочники при старте
    logger.info("Загружаю справочники из Drebedengi...")
    try:
        load_cache()
    except Exception as e:
        logger.warning("Не удалось загрузить справочники при старте: %s", e)

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_entry))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Бот запущен.")
    app.run_polling()


if __name__ == "__main__":
    main()
