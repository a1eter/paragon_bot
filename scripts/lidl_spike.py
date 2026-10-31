#!/usr/bin/env python3
"""
Спайк: логин Lidl Plus → последний чек → текст → parse_lidl_receipt.

Примеры:
  python scripts/lidl_spike.py login --account me
  python scripts/lidl_spike.py fetch  --account me
  python scripts/lidl_spike.py fetch  --account me --ticket-id 2400106885...
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lidl_client import (  # noqa: E402
    LidlClient,
    build_login_url,
    exchange_code,
    extract_code,
    save_tokens,
)
from lidl_html import ticket_to_plain  # noqa: E402
from lidl_parser import parse_lidl_receipt  # noqa: E402


OUT_DIR = ROOT / "data" / "lidl_spike"


def _pkce_path(account: str) -> Path:
    return ROOT / "data" / "lidl_tokens" / f"{account}.pkce.json"


def cmd_login(args: argparse.Namespace) -> None:
    tmp = _pkce_path(args.account)

    if args.code:
        if not tmp.exists():
            raise SystemExit(f"Сначала получи URL: python scripts/lidl_spike.py login --account {args.account}")
        pkce = json.loads(tmp.read_text(encoding="utf-8"))
        code = extract_code(args.code)
        tokens = exchange_code(code, pkce["code_verifier"])
        path = save_tokens(
            args.account,
            tokens,
            keep={
                "country": pkce.get("country", args.country).upper(),
                "language": pkce.get("language", args.language).lower(),
            },
        )
        tmp.unlink(missing_ok=True)
        print(f"OK: токен сохранён в {path}")
        print("Дальше: python scripts/lidl_spike.py fetch --account", args.account)
        return

    url, verifier = build_login_url(country=args.country, language=args.language)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(
        json.dumps({"code_verifier": verifier, "country": args.country, "language": args.language}),
        encoding="utf-8",
    )

    print()
    print("=" * 72)
    print("1) Открой этот URL в Chrome (под своим аккаунтом Lidl Plus):")
    print()
    print(url)
    print()
    print("2) Залогинься как обычно (email/пароль, SMS если попросит, captcha).")
    print("3) Браузер НЕ сможет открыть com.lidlplus.app://callback?... — это ОК.")
    print("   Скопируй ПОЛНЫЙ адрес из адресной строки (или из DevTools -> Network -> callback).")
    print("4) Пришли мне эту строку ИЛИ выполни:")
    print(f'   python scripts/lidl_spike.py login --account {args.account} --code "ВСТАВЬ_СЮДА"')
    print("=" * 72)

    if not args.no_prompt:
        print()
        pasted = input("Callback URL или code (Enter = выйти и вставить позже): ").strip()
        if not pasted:
            print("PKCE сохранён. Заверши логин командой с --code.")
            return
        args.code = pasted
        cmd_login(args)


def cmd_fetch(args: argparse.Namespace) -> None:
    client = LidlClient(args.account)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.ticket_id:
        ticket_id = args.ticket_id
        summary = {"id": ticket_id}
    else:
        page = client.list_tickets(page=1)
        tickets = page.get("tickets") or []
        if not tickets:
            print("Список чеков пуст.")
            return
        summary = tickets[0]
        ticket_id = summary["id"]
        print(f"Всего чеков (totalCount): {page.get('totalCount')}")
        print(f"Последний: id={ticket_id} date={summary.get('date')} total={summary.get('totalAmount')}")
        print()
        print("Топ-5:")
        for t in tickets[:5]:
            print(f"  {t.get('id')}  {t.get('date')}  {t.get('totalAmount')}")
        print()

    detail = client.ticket_detail(ticket_id)
    raw_path = OUT_DIR / f"{ticket_id}.raw.json"
    # Не светим токены; сам чек — личные данные, локально ок
    raw_path.write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Raw JSON → {raw_path}")

    html = detail.get("htmlPrintedReceipt") or ""
    if html:
        html_path = OUT_DIR / f"{ticket_id}.html"
        html_path.write_text(html, encoding="utf-8")
        print(f"HTML       → {html_path}  ({len(html)} chars)")

    plain, source = ticket_to_plain(detail)
    plain_path = OUT_DIR / f"{ticket_id}.plain.txt"
    plain_path.write_text(plain, encoding="utf-8")
    print(f"Plain text → {plain_path}  (source={source}, {len(plain)} chars)")
    print()
    print("--- текст, который ушёл бы в parse_lidl_receipt ---")
    print(plain)
    print("--- конец ---")
    print()

    if not plain.strip():
        print("Пустой текст — парсеру нечего есть. Смотри raw JSON.")
        return

    parsed = parse_lidl_receipt(plain)
    print("=== результат parse_lidl_receipt ===")
    print(f"date={parsed.get('date')}  time={parsed.get('time')}")
    print(f"suma_pln={parsed.get('suma_pln')}  suma_final={parsed.get('suma_final')}  zwrot={parsed.get('zwrot')}")
    if parsed.get("warnings"):
        print("warnings:", parsed["warnings"])
    print(f"items ({len(parsed['items'])}):")
    for i, it in enumerate(parsed["items"], 1):
        flag = " [kaucja]" if it.get("is_kaucja") else ""
        print(f"  {i:2}. {it['name'][:50]:50}  {it['price']:8.2f} → adj {it['adjusted_price']:8.2f}{flag}")

    # Сводка ключей ответа API (без огромного HTML)
    keys = sorted(detail.keys())
    print()
    print("Ключи в detail:", ", ".join(keys))
    print(f"itemsLine: {type(detail.get('itemsLine')).__name__} len={len(detail['itemsLine']) if isinstance(detail.get('itemsLine'), list) else 'n/a'}")
    print(f"htmlPrintedReceipt: {len(html)} chars")


def main() -> None:
    p = argparse.ArgumentParser(description="Lidl Plus spike")
    sub = p.add_subparsers(dest="cmd", required=True)

    login = sub.add_parser("login", help="Одноразовый OAuth в браузере")
    login.add_argument("--account", default="me")
    login.add_argument("--country", default="PL")
    login.add_argument("--language", default="pl")
    login.add_argument("--code", default=None, help="Callback URL или code после логина")
    login.add_argument("--no-prompt", action="store_true", help="Только напечатать URL, не ждать ввода")
    login.set_defaults(func=cmd_login)

    fetch = sub.add_parser("fetch", help="Скачать последний (или указанный) чек и прогнать парсер")
    fetch.add_argument("--account", default="me")
    fetch.add_argument("--ticket-id", default=None)
    fetch.set_defaults(func=cmd_fetch)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
