"""
Синхронизация чеков Lidl Plus.

seen (data/lidl_seen.json) — только чеки, уже отправленные в Telegram.
Автосинк: всё новее первого known (newest-first).
История (/lidl_old): первый чек, которого ещё нет в seen (глубже по времени).
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from lidl_client import LidlClient, token_path
from lidl_html import ticket_to_plain

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent / "data"
SEEN_PATH = DATA_DIR / "lidl_seen.json"
MIGRATED_FLAG = DATA_DIR / "lidl_seen_migrated_v2"


@dataclass
class NewReceipt:
    account_id: str
    ticket_id: str
    date: Optional[str]
    total: Optional[str]
    plain: str


def load_seen() -> dict:
    _maybe_migrate_seen()
    if not SEEN_PATH.exists():
        return {}
    try:
        return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Не удалось прочитать %s: %s", SEEN_PATH, e)
        return {}


def save_seen(seen: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")


def _maybe_migrate_seen() -> None:
    """
    Старый bootstrap писал в seen всю первую страницу без отправки.
    Оставляем только последний id на аккаунт (реально ушедший в Telegram).
    """
    if MIGRATED_FLAG.exists() or not SEEN_PATH.exists():
        return
    try:
        seen = json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    changed = False
    new_seen: dict = {}
    for aid, ids in seen.items():
        if not isinstance(ids, list) or not ids:
            new_seen[aid] = ids
            continue
        if len(ids) > 1:
            new_seen[aid] = [ids[-1]]
            changed = True
            logger.info(
                "Lidl [%s]: migrate seen %d → 1 (оставлен %s)",
                aid,
                len(ids),
                ids[-1],
            )
        else:
            new_seen[aid] = ids
    if changed:
        save_seen(new_seen)
    MIGRATED_FLAG.write_text("v2\n", encoding="utf-8")


def mark_seen(account_id: str, ticket_id: str) -> None:
    seen = load_seen()
    ids = list(seen.get(account_id, []))
    if ticket_id not in ids:
        ids.append(ticket_id)
        seen[account_id] = ids[-2000:]
        save_seen(seen)


def mark_seen_matching_parsed(
    account_id: str,
    *,
    date: Optional[str],
    time: Optional[str],
    total: Optional[float],
    max_pages: int = 3,
) -> Optional[str]:
    """
    Если вставленный текст чека совпал с билетом Lidl Plus — пометить seen,
    чтобы автосинк/maybe_push не прислал тот же чек второй раз.
    Возвращает ticket_id при успехе.
    """
    if not account_has_token(account_id):
        return None
    date_prefix = (date or "")[:10]
    if not date_prefix:
        return None

    want_total = None
    if total is not None:
        try:
            want_total = round(float(total), 2)
        except (TypeError, ValueError):
            want_total = None

    client = LidlClient(account_id)
    for page_no in range(1, max_pages + 1):
        page = client.list_tickets(page=page_no)
        tickets = page.get("tickets") or []
        if not tickets:
            break
        for t in tickets:
            tid = t.get("id")
            if not tid:
                continue
            t_date = str(t.get("date") or "")
            # API: 2026-09-28T19:10:46+00:00; парсер: YYYY-MM-DD
            if t_date[:10] != date_prefix:
                continue
            if want_total is not None:
                try:
                    t_total = round(float(t.get("totalAmount")), 2)
                except (TypeError, ValueError):
                    continue
                if t_total != want_total:
                    continue
            # дата+сумма достаточно (редко два одинаковых чека в один день)
            mark_seen(account_id, tid)
            logger.info(
                "Lidl [%s]: mark_seen по вставленному тексту → %s (%s, %s)",
                account_id,
                tid,
                date_prefix,
                want_total,
            )
            return tid
    return None


def parse_accounts_env(raw: str) -> list[tuple[str, int]]:
    """'me:111,wife:222' → [('me', 111), ('wife', 222)]"""
    result = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        aid, tid = part.split(":", 1)
        aid, tid = aid.strip(), tid.strip()
        if aid and tid.isdigit():
            result.append((aid, int(tid)))
    return result


def account_for_telegram(raw: str, telegram_id: int) -> Optional[str]:
    for aid, tid in parse_accounts_env(raw):
        if tid == telegram_id:
            return aid
    return None


def account_has_token(account_id: str) -> bool:
    return token_path(account_id).exists()


def _detail_to_receipt(account_id: str, summary: dict, client: LidlClient) -> Optional[NewReceipt]:
    tid = summary["id"]
    try:
        detail = client.ticket_detail(tid)
        plain, source = ticket_to_plain(detail)
        if not plain.strip():
            logger.warning("Lidl [%s]: пустой текст чека %s (source=%s)", account_id, tid, source)
            return None
        return NewReceipt(
            account_id=account_id,
            ticket_id=tid,
            date=str(summary.get("date") or detail.get("date") or ""),
            total=str(summary.get("totalAmount") or detail.get("totalAmount") or ""),
            plain=plain,
        )
    except Exception:
        logger.exception("Lidl [%s]: ошибка детализации %s", account_id, tid)
        return None


def collect_new_receipts(account_id: str, *, max_new: int = 5) -> list[NewReceipt]:
    """
    Новые чеки для автосинка (новее уже отправленных).
    Пустой seen → только самый новый (без пометки всей истории).
    """
    if not account_has_token(account_id):
        raise FileNotFoundError(f"Нет токена для аккаунта '{account_id}'")

    client = LidlClient(account_id)
    page = client.list_tickets(page=1)
    tickets = page.get("tickets") or []
    if not tickets:
        return []

    known = set(load_seen().get(account_id, []))

    if not known:
        logger.info("Lidl [%s]: первый автосинк — отдаём только последний %s", account_id, tickets[0].get("id"))
        new_summaries = [tickets[0]]
    else:
        new_summaries = []
        for t in tickets:
            tid = t.get("id")
            if not tid:
                continue
            if tid in known:
                break
            new_summaries.append(t)
        if not new_summaries:
            logger.info("Lidl [%s]: новых чеков нет", account_id)
            return []

    new_summaries = list(reversed(new_summaries[:max_new]))
    results: list[NewReceipt] = []
    for summary in new_summaries:
        r = _detail_to_receipt(account_id, summary, client)
        if r:
            results.append(r)
    return results


def fetch_receipt(account_id: str, ticket_id: str) -> Optional[NewReceipt]:
    """Загрузить один чек по id."""
    if not account_has_token(account_id):
        raise FileNotFoundError(f"Нет токена для аккаунта '{account_id}'")
    client = LidlClient(account_id)
    return _detail_to_receipt(account_id, {"id": ticket_id}, client)


@dataclass
class UnseenPage:
    items: list[dict]  # raw ticket summaries from API
    offset: int
    has_more: bool
    total_unseen_approx: Optional[int] = None


def list_unseen_summaries(
    account_id: str,
    *,
    offset: int = 0,
    limit: int = 7,
    max_pages: int = 50,
) -> UnseenPage:
    """
    Неразобранные чеки newest→older, страница limit штук начиная с offset.
    """
    if not account_has_token(account_id):
        raise FileNotFoundError(f"Нет токена для аккаунта '{account_id}'")

    client = LidlClient(account_id)
    known = set(load_seen().get(account_id, []))

    page1 = client.list_tickets(page=1)
    api_tickets = page1.get("tickets") or []
    if not api_tickets:
        return UnseenPage(items=[], offset=offset, has_more=False)

    size = int(page1.get("size") or len(api_tickets) or 20)
    total = int(page1.get("totalCount") or 0)
    total_pages = max(1, math.ceil(total / size)) if size else 1
    total_pages = min(total_pages, max_pages)

    unseen: list[dict] = []
    need = offset + limit + 1  # +1 чтобы знать has_more
    for page_num in range(1, total_pages + 1):
        page = page1 if page_num == 1 else client.list_tickets(page=page_num)
        for t in page.get("tickets") or []:
            tid = t.get("id")
            if not tid or tid in known:
                continue
            unseen.append(t)
            if len(unseen) >= need:
                break
        if len(unseen) >= need:
            break

    chunk = unseen[offset : offset + limit]
    has_more = len(unseen) > offset + limit
    return UnseenPage(items=chunk, offset=offset, has_more=has_more)
