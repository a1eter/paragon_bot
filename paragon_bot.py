"""
Telegram-бот для добавления записей в Drebedengi.

Формат ввода (примеры):
  кофе 150
  зарплата 50000 доход
  такси 350 #транспорт
  обед 450 #еда кафе рядом с работой
"""

import hashlib
import logging
from logging.handlers import RotatingFileHandler
import os
import re
from datetime import datetime

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
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

class TruncatingFileHandler(RotatingFileHandler):
    """При ротации обнуляет файл вместо переименования."""
    def doRollover(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        open(self.baseFilename, 'w', encoding=self.encoding).close()
        if not self.delay:
            self.stream = self._open()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        TruncatingFileHandler(
            os.path.join(os.path.dirname(__file__), "paragon_bot.log"),
            maxBytes=10*1024*1024,
            backupCount=0,
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

LIDL_ACCOUNTS_RAW = os.getenv("LIDL_ACCOUNTS", "")
LIDL_SYNC_INTERVAL_HOURS = float(os.getenv("LIDL_SYNC_INTERVAL_HOURS", "1"))
LIDL_SYNC_TZ = os.getenv("LIDL_SYNC_TZ", "Europe/Warsaw")

dd = DrebedengiClient(DD_API_ID, DD_LOGIN, DD_PASS)

# Простой кэш справочников (загружается один раз при старте /refresh)
_cache: dict = {}

# Дедупликация Telegram-апдейтов (защита от повторной доставки)
_seen_update_ids: set = set()


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

BTN_LIDL_OLD = "📦 Разобрать старый чек"


def _main_reply_keyboard() -> ReplyKeyboardMarkup:
    """Постоянная клавиатура внизу чата."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton(BTN_LIDL_OLD)]],
        resize_keyboard=True,
        is_persistent=True,
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else "?"
    await update.message.reply_text(
        f"Telegram ID: <code>{uid}</code>\n\n"
        f"Кнопка «{BTN_LIDL_OLD}» всегда внизу чата.\n"
        "Новые чеки Lidl приходят сами раз в час.",
        parse_mode="HTML",
        reply_markup=_main_reply_keyboard(),
    )


# ──────────────────────────────────────────────────────────────
# Помощники для классификации
# ──────────────────────────────────────────────────────────────

# Избранные счета для быстрого выбора (id → метка кнопки)
FAVORITE_PLACES = [
    ("16318720", "Revolut"),
    ("15310091", "PKO"),
    ("16299773", "Erste A"),
    ("16395412", "Erste M"),
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


def _category_keyboard(cache: dict, with_kw_edit: bool = False) -> InlineKeyboardMarkup:
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
    if with_kw_edit:
        buttons.append([InlineKeyboardButton("✏️ Изменить ключевое слово", callback_data="cat_kw_edit")])
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
        f"Сумма: {item['amount']:.2f} {item['cur_code']}\n"
        f"🔑 Ключ для правила: <code>{item['keyword']}</code>\n\n"
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

    # Кауция — в конец
    KAUCJA_NAMES = {"kaucja", "kаucia", "залог", "кауция"}
    main_groups = {k: v for k, v in groups.items() if k.lower() not in KAUCJA_NAMES}
    tail_groups = {k: v for k, v in groups.items() if k.lower() in KAUCJA_NAMES}

    lines = [f"📅 {date_str}\n"]
    for cat_name, items in {**main_groups, **tail_groups}.items():
        emoji = _cat_emoji(cat_name)
        lines.append(f"{emoji} <b>{cat_name}</b>")
        for comment, amount, code in items:
            lines.append(f"  {comment}  {amount:.2f} {code}")
        lines.append("")

    lines.append("─" * 21)
    lines.append(f"<b>Итого: {total:.2f} {cur_code}</b>")
    lines.append(f"💳 {place_name}")

    return "\n".join(lines)



def _lidl_stable_client_id(parsed: dict) -> int:
    """
    Стабильный client_id по содержимому чека.
    Повторная отправка того же чека в Drebedengi обновит записи, а не создаст дубликат.
    """
    items = parsed.get("items") or []
    parts = [
        "lidl",
        str(parsed.get("date") or ""),
        str(parsed.get("time") or ""),
        f"{sum(float(i.get('adjusted_price') or 0) for i in items):.2f}",
        *(f"{i.get('name')}|{i.get('adjusted_price')}" for i in items),
    ]
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return 1_000_000_000 + (int(digest[:12], 16) % 1_000_000_000)


def _lidl_parsed_total(parsed: dict) -> float | None:
    items = parsed.get("items") or []
    if not items:
        return None
    return round(sum(float(i.get("adjusted_price") or 0) for i in items), 2)


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


async def start_lidl_receipt_flow(bot, chat_id: int, user_data: dict, text: str) -> bool:
    """
    Запускает UI чека Лидл (выбор счёта → категории).
    Возвращает True если чек принят, False если распознать не удалось.
    """
    from lidl_parser import parse_lidl_receipt

    cache = get_cache()
    if not cache:
        await bot.send_message(chat_id, "Сначала загрузи справочники: /refresh")
        return False

    parsed_receipt = parse_lidl_receipt(text)
    if not parsed_receipt["items"]:
        await bot.send_message(chat_id, "Не удалось распознать позиции в чеке.")
        return False

    base_client_id = _lidl_stable_client_id(parsed_receipt)
    records, valid_lines, all_warnings = _lidl_to_records(parsed_receipt, cache, base_client_id)
    if not records:
        await bot.send_message(chat_id, "Не удалось преобразовать чек в записи.")
        return False

    classify_queue = []
    for i, (record, line) in enumerate(zip(records, valid_lines)):
        if _is_default_category(record, cache):
            cur = cache["currencies"].get(str(record["currency_id"])) or cache["currencies"].get(record["currency_id"])
            cur_code = cur.get("code", "?") if cur else "?"
            comment_words = [w for w in record["comment"].split() if not (w.startswith("[") and w.endswith("]"))]
            keyword = " ".join(comment_words[:2]) if comment_words else " ".join(line.split()[:2])
            classify_queue.append({
                "idx":      i,
                "keyword":  keyword,
                "line":     line,
                "amount":   record["sum"] / 100,
                "cur_code": cur_code,
            })

    user_data["pending_records"] = records
    user_data["pending_warnings"] = all_warnings
    user_data["pending_lidl_fingerprint"] = {
        "date": parsed_receipt.get("date"),
        "time": parsed_receipt.get("time"),
        "total": _lidl_parsed_total(parsed_receipt),
    }
    if classify_queue:
        user_data["classify_queue"] = classify_queue
    else:
        user_data.pop("classify_queue", None)

    n = len(records)
    total = sum(r["sum"] / 100 for r in records)
    cur = cache["currencies"].get(str(records[0]["currency_id"])) or cache["currencies"].get(records[0]["currency_id"])
    cur_code = cur.get("code", "?") if cur else "?"
    summary = f"{n} {'запись' if n == 1 else 'записи' if 2 <= n <= 4 else 'записей'}, итого {total:.2f} {cur_code}"
    date_line = f"\n📅 {parsed_receipt['date']}" if parsed_receipt.get("date") else ""
    await bot.send_message(
        chat_id,
        f"🧾 {summary}{date_line}\n\n💳 <b>С какого счёта?</b>",
        parse_mode="HTML",
        reply_markup=_account_keyboard(),
    )
    return True


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

    # Защита от повторной доставки одного и того же апдейта Telegram-ом
    uid = update.update_id
    if uid in _seen_update_ids:
        logger.warning("Дублированный update_id=%d, пропускаем", uid)
        return
    _seen_update_ids.add(uid)
    if len(_seen_update_ids) > 2000:
        _seen_update_ids.clear()

    text = update.message.text.strip()

    # Постоянная кнопка reply-клавиатуры
    if text == BTN_LIDL_OLD:
        await update.message.reply_text("Загружаю список чеков…")
        await show_history_menu(context.application, update.effective_user.id, offset=0)
        return

    # Ответ на /lidl_auth — вставка callback URL
    if await _maybe_lidl_auth_paste(update, context, text):
        return

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
            kb = _category_keyboard(cache, with_kw_edit=True)
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

    # ── Режим редактирования ключевого слова ──────────────────
    if context.user_data.get("kw_edit_active"):
        context.user_data.pop("kw_edit_active", None)
        classify_queue = context.user_data.get("classify_queue", [])
        if classify_queue:
            classify_queue[0]["keyword"] = text.strip()
        chat_id = context.user_data.pop("kw_edit_chat_id", None)
        msg_id = context.user_data.pop("kw_edit_msg_id", None)
        cache = get_cache()
        current = classify_queue[0] if classify_queue else None
        try:
            await update.message.delete()
        except Exception:
            pass
        if current and chat_id and msg_id:
            await context.bot.edit_message_text(
                _classify_prompt(current, len(classify_queue)),
                chat_id=chat_id,
                message_id=msg_id,
                parse_mode="HTML",
                reply_markup=_category_keyboard(cache, with_kw_edit=True),
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
    _LIDL_MARKER_RE = re.compile(
        r'Suma PLN|Opakowania zwrotne|Razem\s+[\d,]+',
        re.IGNORECASE,
    )
    _LIDL_END_RE = re.compile(r'Suma PLN|Razem\s+[\d,]', re.IGNORECASE)

    # Подклеиваем буфер от предыдущего фрагмента, если есть
    lidl_buffer = context.user_data.get("lidl_buffer", "")
    is_continuation = bool(lidl_buffer)
    effective_text = (lidl_buffer + "\n" + text) if is_continuation else text

    if _LIDL_MARKER_RE.search(effective_text) or _LIDL_ITEM_RE.search(effective_text):
        # Фрагмент без товарных строк — хвост чека, уже обработанного ранее
        if not _LIDL_ITEM_RE.search(effective_text):
            logger.debug("Lidl-хвост без товарных строк, пропускаем")
            context.user_data.pop("lidl_buffer", None)
            return

        # Чек ещё не завершён (нет Suma PLN / Razem) — буферизуем и ждём продолжения
        if not _LIDL_END_RE.search(effective_text):
            logger.debug("Lidl-фрагмент без финальной суммы, буферизуем (%d симв.)", len(effective_text))
            context.user_data["lidl_buffer"] = effective_text
            return

        # Чек полный — сбрасываем буфер и парсим
        context.user_data.pop("lidl_buffer", None)
        ok = await start_lidl_receipt_flow(
            context.bot,
            update.effective_chat.id,
            context.user_data,
            effective_text,
        )
        # Вставка текстом не шла через Lidl API — пометим совпавший ticket,
        # иначе maybe_push/hourly пришлют тот же чек ещё раз.
        if ok and update.effective_user:
            fp = context.user_data.get("pending_lidl_fingerprint") or {}
            try:
                from lidl_sync import account_for_telegram, mark_seen_matching_parsed
                aid = account_for_telegram(LIDL_ACCOUNTS_RAW, update.effective_user.id)
                if aid:
                    mark_seen_matching_parsed(
                        aid,
                        date=fp.get("date"),
                        time=fp.get("time"),
                        total=fp.get("total"),
                    )
            except Exception:
                logger.exception("Lidl mark_seen после вставки текста")
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
            # Берём первые два слова комментария, пропуская тег [LIDL]
            comment_words = [w for w in record["comment"].split() if not (w.startswith("[") and w.endswith("]"))]
            keyword = " ".join(comment_words[:2]) if comment_words else " ".join(line.split()[:2])
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


async def _auto_send(query, records: list, cache: dict, context: ContextTypes.DEFAULT_TYPE | None = None) -> None:
    """Отправляет записи на Drebedengi и показывает результат."""
    if context is not None:
        if context.user_data.get("dd_sending"):
            logger.warning("Пропуск повторного _auto_send (уже идёт запись в DD)")
            return
        context.user_data["dd_sending"] = True
    try:
        if not records:
            await query.edit_message_text("Запись устарела, попробуй снова.")
            return
        logger.info(
            "DD set_record_list: %d записей, group=%s, date=%s",
            len(records),
            records[0].get("group_id"),
            (records[0].get("operation_date") or "")[:16],
        )
        result, raw_response = dd.set_record_list(records)
        if result is None:
            logger.warning("Ответ сервера не распознан. raw=\n%s", raw_response)
            await query.edit_message_text(
                f"❓ Сервер не подтвердил запись. Ответ:\n<code>{raw_response[:600]}</code>",
                parse_mode="HTML",
            )
        else:
            msg = _build_success_message(records, cache)
            await query.edit_message_text(msg, parse_mode="HTML")
            # Сразу подтянуть следующий новый чек Lidl, если есть в очереди
            if context and query.from_user:
                await maybe_push_next_lidl_receipt(context.application, query.from_user.id)
    except Exception as e:
        await query.edit_message_text(f"❌ Ошибка при записи: {e}")
    finally:
        if context is not None:
            context.user_data.pop("dd_sending", None)
            context.user_data.pop("pending_lidl_fingerprint", None)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == "lidl_old":
        if not is_allowed(update):
            return
        await query.edit_message_text("Загружаю список чеков…")
        await show_history_menu(
            context.application,
            update.effective_user.id,
            offset=0,
            edit_message=query.message,
        )
        return

    if query.data == "lidl_hx":
        await query.edit_message_text("Закрыто.")
        return

    if query.data.startswith("lidl_hp:"):
        if not is_allowed(update):
            return
        try:
            offset = int(query.data.split(":", 1)[1])
        except ValueError:
            await query.edit_message_text("Ошибка листания.")
            return
        await query.edit_message_text("Загружаю…")
        await show_history_menu(
            context.application,
            update.effective_user.id,
            offset=offset,
            edit_message=query.message,
        )
        return

    if query.data.startswith("lidl_hs:"):
        if not is_allowed(update):
            return
        ticket_id = query.data.split(":", 1)[1]
        await query.edit_message_text(f"Загружаю чек…")
        await push_selected_history_receipt(
            context.application,
            update.effective_user.id,
            ticket_id,
        )
        return

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
                reply_markup=_category_keyboard(cache, with_kw_edit=True),
            )
        else:
            records = context.user_data.pop("pending_records", [])
            context.user_data.pop("pending_warnings", None)
            await _auto_send(query, records, cache, context)
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
                reply_markup=_category_keyboard(cache, with_kw_edit=True),
            )
            return

        if query.data == "cat_kw_edit":
            context.user_data["kw_edit_active"] = True
            context.user_data["kw_edit_chat_id"] = query.message.chat_id
            context.user_data["kw_edit_msg_id"] = query.message.message_id
            await query.edit_message_text(
                _classify_prompt(current, len(classify_queue))
                + "\n\n✏️ <i>Напиши новое ключевое слово:</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Отмена", callback_data="cat_kw_back")]]),
            )
            return

        if query.data == "cat_kw_back":
            context.user_data.pop("kw_edit_active", None)
            await query.edit_message_text(
                _classify_prompt(current, len(classify_queue)),
                parse_mode="HTML",
                reply_markup=_category_keyboard(cache, with_kw_edit=True),
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
                reply_markup=_category_keyboard(cache, with_kw_edit=True),
            )
        else:
            records = context.user_data.pop("pending_records", [])
            context.user_data.pop("pending_warnings", None)
            context.user_data.pop("classify_queue", None)
            await _auto_send(query, records, cache, context)


# ──────────────────────────────────────────────────────────────
# Lidl Plus: автосинк чеков
# ──────────────────────────────────────────────────────────────

def _lidl_accounts() -> list[tuple[str, int]]:
    from lidl_sync import parse_accounts_env
    return parse_accounts_env(LIDL_ACCOUNTS_RAW)


def _lidl_old_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📦 Разобрать старый чек", callback_data="lidl_old")],
    ])


def _fmt_receipt_btn(summary: dict) -> str:
    date = str(summary.get("date") or "")[:10] or "?"
    total = summary.get("totalAmount")
    total_s = f"{total} zł" if total is not None else "?"
    return f"{date} · {total_s}"


def _history_pick_keyboard(items: list[dict], offset: int, has_more: bool) -> InlineKeyboardMarkup:
    rows = []
    for t in items:
        tid = t.get("id")
        if not tid:
            continue
        rows.append([
            InlineKeyboardButton(
                _fmt_receipt_btn(t),
                callback_data=f"lidl_hs:{tid}",
            )
        ])
    nav = []
    if offset > 0:
        prev = max(0, offset - 7)
        nav.append(InlineKeyboardButton("◀️ Назад", callback_data=f"lidl_hp:{prev}"))
    if has_more:
        nav.append(InlineKeyboardButton("Следующие ▶️", callback_data=f"lidl_hp:{offset + 7}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("✖️ Закрыть", callback_data="lidl_hx")])
    return InlineKeyboardMarkup(rows)


async def _push_receipt_to_user(
    app,
    telegram_id: int,
    receipt,
    preface: str = "",
    *,
    offer_history: bool = True,
) -> bool:
    """Отправляет один чек пользователю. False если у него висит незавершённый."""
    # В PTB 21 app.user_data — MappingProxy (read-only); писать нужно в _user_data
    ud = app._user_data[telegram_id]
    if ud.get("pending_records"):
        await app.bot.send_message(
            telegram_id,
            "⏳ Сначала закончи текущий чек (счёт/категории), потом можно брать следующий.",
        )
        return False

    header = preface or "🛒 Новый чек из Lidl Plus"
    meta = []
    if receipt.date:
        meta.append(str(receipt.date)[:10])
    if receipt.total:
        meta.append(f"{receipt.total} zł")
    if meta:
        header += " (" + ", ".join(meta) + ")"
    await app.bot.send_message(telegram_id, header)
    ok = await start_lidl_receipt_flow(app.bot, telegram_id, ud, receipt.plain)
    if ok:
        from lidl_sync import mark_seen
        mark_seen(receipt.account_id, receipt.ticket_id)
        if offer_history:
            await app.bot.send_message(
                telegram_id,
                "Можно углубиться в историю — кнопка внизу чата.",
                reply_markup=_main_reply_keyboard(),
            )
    return ok


async def show_history_menu(
    app,
    telegram_id: int,
    *,
    offset: int = 0,
    edit_message=None,
) -> None:
    """Показать до 7 неразобранных чеков для выбора."""
    from lidl_sync import account_for_telegram, account_has_token, list_unseen_summaries

    account_id = account_for_telegram(LIDL_ACCOUNTS_RAW, telegram_id)
    if not account_id:
        text = "Твой Telegram id не привязан в LIDL_ACCOUNTS (.env)."
        if edit_message:
            await edit_message.edit_text(text)
        else:
            await app.bot.send_message(telegram_id, text)
        return
    if not account_has_token(account_id):
        text = f"Нет токена Lidl → /lidl_auth {account_id}"
        if edit_message:
            await edit_message.edit_text(text)
        else:
            await app.bot.send_message(telegram_id, text)
        return

    ud = app._user_data[telegram_id]
    if ud.get("pending_records"):
        text = "⏳ Сначала закончи текущий чек, потом выбирай старый."
        if edit_message:
            await edit_message.edit_text(text)
        else:
            await app.bot.send_message(telegram_id, text)
        return

    try:
        page = list_unseen_summaries(account_id, offset=offset, limit=7)
    except Exception as e:
        logger.exception("lidl history menu [%s]", account_id)
        text = f"Ошибка Lidl: {e}"
        if edit_message:
            await edit_message.edit_text(text)
        else:
            await app.bot.send_message(telegram_id, text)
        return

    if not page.items:
        text = (
            "✅ Неразобранных чеков больше нет."
            if offset == 0
            else "Дальше неразобранных нет. Нажми «Назад» или закрой."
        )
        if edit_message:
            await edit_message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀️ Назад", callback_data=f"lidl_hp:{max(0, offset - 7)}")]
                    if offset > 0 else [InlineKeyboardButton("✖️ Закрыть", callback_data="lidl_hx")]
                ]),
            )
        else:
            await app.bot.send_message(telegram_id, text, reply_markup=_main_reply_keyboard())
        return

    n0 = offset + 1
    n1 = offset + len(page.items)
    text = (
        f"📦 Выбери чек для разбора ({n0}–{n1}"
        + (", есть ещё…" if page.has_more else "")
        + "):"
    )
    kb = _history_pick_keyboard(page.items, offset, page.has_more)
    if edit_message:
        await edit_message.edit_text(text, reply_markup=kb)
    else:
        await app.bot.send_message(telegram_id, text, reply_markup=kb)


async def push_selected_history_receipt(app, telegram_id: int, ticket_id: str) -> None:
    from lidl_sync import account_for_telegram, account_has_token, fetch_receipt

    account_id = account_for_telegram(LIDL_ACCOUNTS_RAW, telegram_id)
    if not account_id or not account_has_token(account_id):
        await app.bot.send_message(telegram_id, "Нет привязки/токена Lidl.")
        return

    ud = app._user_data[telegram_id]
    if ud.get("pending_records"):
        await app.bot.send_message(
            telegram_id,
            "⏳ Сначала закончи текущий чек.",
        )
        return

    try:
        receipt = fetch_receipt(account_id, ticket_id)
    except Exception as e:
        logger.exception("fetch receipt")
        await app.bot.send_message(telegram_id, f"Ошибка: {e}")
        return

    if not receipt:
        await app.bot.send_message(telegram_id, "Не удалось загрузить чек.")
        return

    await _push_receipt_to_user(
        app,
        telegram_id,
        receipt,
        preface="📦 Старый чек из Lidl Plus",
        offer_history=True,
    )


async def maybe_push_next_lidl_receipt(app, telegram_id: int) -> None:
    """
    После успешного разбора чека: если у аккаунта есть новые непрочитанные —
    сразу отправить один следующий (не ждать часового синка).
    """
    from lidl_sync import account_for_telegram, account_has_token, collect_new_receipts

    account_id = account_for_telegram(LIDL_ACCOUNTS_RAW, telegram_id)
    if not account_id or not account_has_token(account_id):
        return

    ud = app._user_data[telegram_id]
    if ud.get("pending_records"):
        return

    try:
        receipts = collect_new_receipts(account_id, max_new=1)
    except Exception:
        logger.exception("Lidl immediate next [%s]", account_id)
        return

    if not receipts:
        return

    r = receipts[0]
    logger.info("Lidl [%s]: сразу следующий чек %s", account_id, r.ticket_id)
    await _push_receipt_to_user(
        app,
        telegram_id,
        r,
        preface="🛒 Следующий чек из Lidl Plus",
        offer_history=True,
    )


async def push_history_receipt_for_user(app, telegram_id: int) -> None:
    """Совместимость: открыть меню выбора старых чеков."""
    await show_history_menu(app, telegram_id, offset=0)


async def run_lidl_sync(app, *, notify_chat_id: int | None = None) -> None:
    """Прогон синка по всем аккаунтам из LIDL_ACCOUNTS."""
    from lidl_sync import account_has_token, collect_new_receipts

    accounts = _lidl_accounts()
    if not accounts:
        msg = "LIDL_ACCOUNTS не задан в .env (формат: me:TELEGRAM_ID,wife:TELEGRAM_ID)"
        logger.warning(msg)
        if notify_chat_id:
            await app.bot.send_message(notify_chat_id, msg)
        return

    lines_status = []
    for account_id, telegram_id in accounts:
        if not account_has_token(account_id):
            tip = f"[{account_id}] нет токена → /lidl_auth {account_id}"
            lines_status.append(tip)
            try:
                await app.bot.send_message(telegram_id, f"⚠️ Lidl: {tip}")
            except Exception:
                logger.exception("Не удалось написать %s", telegram_id)
            continue
        try:
            receipts = collect_new_receipts(account_id)
        except Exception as e:
            logger.exception("Lidl sync [%s]", account_id)
            err = f"[{account_id}] ошибка: {e}"
            lines_status.append(err)
            try:
                await app.bot.send_message(
                    telegram_id,
                    f"⚠️ Lidl sync: {e}\nЕсли про токен — сделай /lidl_auth {account_id}",
                )
            except Exception:
                pass
            continue

        if not receipts:
            lines_status.append(f"[{account_id}] новых нет")
            continue

        sent = 0
        for r in receipts:
            ok = await _push_receipt_to_user(app, telegram_id, r)
            if ok:
                sent += 1
            else:
                break  # pending — остальные завтра
        lines_status.append(f"[{account_id}] отправлено {sent}/{len(receipts)}")

    summary = "Lidl sync: " + "; ".join(lines_status)
    logger.info(summary)
    if notify_chat_id:
        await app.bot.send_message(notify_chat_id, summary)


async def lidl_daily_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_lidl_sync(context.application)


async def cmd_lidl_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    await update.message.reply_text("Синхронизирую Lidl…")
    await run_lidl_sync(context.application, notify_chat_id=update.effective_chat.id)


async def cmd_lidl_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    from lidl_sync import account_has_token, load_seen

    accounts = _lidl_accounts()
    seen = load_seen()
    if not accounts:
        await update.message.reply_text("LIDL_ACCOUNTS пуст в .env")
        return
    lines = [
        f"Синк: каждые {LIDL_SYNC_INTERVAL_HOURS:g} ч ({LIDL_SYNC_TZ})",
    ]
    for aid, tid in accounts:
        tok = "токен ✓" if account_has_token(aid) else "нет токена"
        n = len(seen.get(aid, []))
        lines.append(f"• <code>{aid}</code> → TG {tid}: {tok}, разобрано={n}")
    lines.append("\n📦 Старые чеки — кнопка внизу или /lidl_old")
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_main_reply_keyboard(),
    )


async def cmd_lidl_old(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    await update.message.reply_text("Загружаю список чеков…")
    await show_history_menu(context.application, update.effective_user.id, offset=0)


async def cmd_lidl_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    from lidl_client import build_login_url, exchange_code, extract_code, save_tokens
    from pathlib import Path
    import json as _json

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Использование:\n"
            "  /lidl_auth me\n"
            "  /lidl_auth me <callback_url_или_code>\n"
            "Сначала без code — получишь ссылку для Chrome."
        )
        return

    account_id = args[0].strip()
    pkce_path = Path(__file__).resolve().parent / "data" / "lidl_tokens" / f"{account_id}.pkce.json"

    if len(args) == 1:
        url, verifier = build_login_url(country="PL", language="pl")
        pkce_path.parent.mkdir(parents=True, exist_ok=True)
        pkce_path.write_text(
            _json.dumps({"code_verifier": verifier, "country": "PL", "language": "pl"}),
            encoding="utf-8",
        )
        context.user_data["lidl_auth_account"] = account_id
        await update.message.reply_text(
            f"Открой в <b>Chrome</b>:\n{url}\n\n"
            "После логина страница «не откроется» — скопируй адрес "
            "<code>com.lidlplus.app://callback?code=...</code> "
            "и пришли следующим сообщением или:\n"
            f"/lidl_auth {account_id} CODE",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    # code / callback в аргументах
    pasted = " ".join(args[1:])
    if not pkce_path.exists():
        await update.message.reply_text(f"Сначала: /lidl_auth {account_id}")
        return
    try:
        pkce = _json.loads(pkce_path.read_text(encoding="utf-8"))
        code = extract_code(pasted)
        tokens = exchange_code(code, pkce["code_verifier"])
        path = save_tokens(
            account_id,
            tokens,
            keep={"country": "PL", "language": "pl"},
        )
        pkce_path.unlink(missing_ok=True)
        context.user_data.pop("lidl_auth_account", None)
        await update.message.reply_text(f"OK: токен сохранён ({path.name}). Можно /lidl_sync")
    except Exception as e:
        await update.message.reply_text(f"Ошибка обмена code: {e}")


# перехват callback URL после /lidl_auth
async def _maybe_lidl_auth_paste(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    account_id = context.user_data.get("lidl_auth_account")
    if not account_id:
        return False
    if "code=" not in text and "com.lidlplus.app://" not in text:
        return False
    context.args = [account_id, text]
    await cmd_lidl_auth(update, context)
    return True


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
    app.add_handler(CommandHandler("lidl_sync", cmd_lidl_sync))
    app.add_handler(CommandHandler("lidl_status", cmd_lidl_status))
    app.add_handler(CommandHandler("lidl_old", cmd_lidl_old))
    app.add_handler(CommandHandler("lidl_auth", cmd_lidl_auth))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_entry))
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Ежедневный синк Lidl
    if app.job_queue is None:
        logger.warning(
            "JobQueue недоступен — поставь python-telegram-bot[job-queue]. "
            "Автосинк Lidl отключён, остаётся /lidl_sync."
        )
    elif _lidl_accounts():
        try:
            seconds = max(300, int(LIDL_SYNC_INTERVAL_HOURS * 3600))
            app.job_queue.run_repeating(
                lidl_daily_job,
                interval=seconds,
                first=60,  # первый прогон через минуту после старта
                name="lidl_hourly_sync",
            )
            logger.info(
                "Lidl sync: каждые %g ч (первый через 60с)",
                LIDL_SYNC_INTERVAL_HOURS,
            )
        except Exception:
            logger.exception("Не удалось запланировать Lidl sync")
    else:
        logger.info("LIDL_ACCOUNTS пуст — ежедневный синк не включён")

    logger.info("Бот запущен.")
    app.run_polling()


if __name__ == "__main__":
    main()
