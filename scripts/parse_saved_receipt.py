#!/usr/bin/env python3
"""Прогон сохранённой страницы чека Lidl через парсер."""
from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lidl_parser import parse_lidl_receipt  # noqa: E402


class _PreText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "br":
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def extract_pre_plain(html: str) -> str:
    m = re.search(r"<pre>(.*?)</pre>", html, re.I | re.S)
    if not m:
        raise SystemExit("В HTML нет <pre> с чеком")
    p = _PreText()
    p.feed(m.group(1))
    p.close()
    lines = [ln.rstrip() for ln in "".join(p.parts).splitlines()]
    return "\n".join(lines).strip() + "\n"


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"c:\Users\Aleter\Desktop\Store Receipts.htm")
    html = src.read_text(encoding="utf-8", errors="replace")
    plain = extract_pre_plain(html)

    out_dir = ROOT / "data" / "lidl_spike"
    out_dir.mkdir(parents=True, exist_ok=True)
    plain_path = out_dir / "receipt.plain.txt"
    plain_path.write_text(plain, encoding="utf-8")

    print("=== PLAIN (что ушло бы в бота) ===")
    print(plain)
    print(f"(сохранено: {plain_path})")
    print("=== parse_lidl_receipt ===")
    parsed = parse_lidl_receipt(plain)
    print(f"date={parsed['date']}  time={parsed['time']}")
    print(f"suma_pln={parsed['suma_pln']}  suma_final={parsed['suma_final']}  zwrot={parsed['zwrot']}")
    print("warnings:", parsed["warnings"])
    print(f"items={len(parsed['items'])}")
    total = 0.0
    for i, it in enumerate(parsed["items"], 1):
        total += it["adjusted_price"]
        print(f"  {i:2}. {it['name'][:42]:42} {it['price']:7.2f}  adj={it['adjusted_price']:7.2f}")
    print(f"sum(items)={total:.2f}")


if __name__ == "__main__":
    main()
