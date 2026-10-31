"""
Конвертация ответа Lidl ticket detail → плоский текст для parse_lidl_receipt.
"""

from __future__ import annotations

import html as html_lib
import re
from html.parser import HTMLParser
from typing import Optional


class _TextExtractor(HTMLParser):
    """Достаёт читаемый текст, сохраняя переносы из <br>/<p>/<pre>/<div>."""

    BLOCK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "pre", "section"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag.lower() in {"p", "div", "tr", "li", "pre", "section"}:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = raw.replace("\xa0", " ")
        lines = [ln.rstrip() for ln in raw.splitlines()]
        # Схлопываем пустые серии, но не убиваем все пустые строки
        out: list[str] = []
        empty = 0
        for ln in lines:
            if not ln.strip():
                empty += 1
                if empty <= 1:
                    out.append("")
            else:
                empty = 0
                out.append(ln)
        return "\n".join(out).strip()


_ARTICLE_RE = re.compile(
    r'<span[^>]*class="[^"]*article[^"]*"[^>]*>(.*?)</span>',
    re.I | re.S,
)
_ATTR_RE = re.compile(r'data-art-description="([^"]*)".*?data-art-quantity="([^"]*)".*?data-unit-price="([^"]*)".*?data-tax-type="([^"]*)"', re.I | re.S)
# Атрибуты могут быть в любом порядке — парсим по отдельности
_DATA_ATTR = re.compile(r'data-([a-z0-9-]+)="([^"]*)"', re.I)


def _pl_money(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def html_articles_to_plain(html: str, *, date: Optional[str] = None, total: Optional[str] = None) -> Optional[str]:
    """
    Если в HTML есть span.article с data-art-* — собираем текст в формате парсера.
    Иначе None (пусть вызывающий fallback'нется на strip).
    """
    articles = list(_ARTICLE_RE.finditer(html))
    if not articles:
        # Иногда class без кавычек / другой порядок — ищем по data-art-description
        if "data-art-description" not in html:
            return None

    # Разбиваем по article spans более надёжно
    chunks = re.split(r'(?=<span[^>]*data-art-description=)', html, flags=re.I)
    items: list[tuple[str, float, float, str]] = []  # name, qty, unit, tax
    for chunk in chunks:
        if "data-art-description" not in chunk.lower():
            continue
        # Берём открывающий тег
        m = re.match(r'<span([^>]*)>', chunk, re.I)
        if not m:
            continue
        attrs = {k.lower(): html_lib.unescape(v) for k, v in _DATA_ATTR.findall(m.group(1))}
        name = attrs.get("data-art-description")
        if not name:
            continue
        qty_s = attrs.get("data-art-quantity", "1") or "1"
        unit_s = attrs.get("data-unit-price", "0") or "0"
        tax = attrs.get("data-tax-type", "C") or "C"
        try:
            qty = float(qty_s.replace(",", "."))
            unit = float(unit_s.replace(",", "."))
        except ValueError:
            continue
        items.append((name.strip(), qty, unit, tax.strip() or "C"))

    if not items:
        return None

    lines: list[str] = []
    if date:
        # date может быть ISO — берём YYYY-MM-DD
        lines.append(date[:10])
    for name, qty, unit, tax in items:
        line_total = qty * unit
        # Весовой: "0,74 kg x 0,89  0,66 C"
        if qty != int(qty) or abs(qty - 1.0) > 1e-6 and qty < 50 and ("kg" in name.lower() or qty < 1):
            # эвристика веса: дробное qty
            if qty != int(qty):
                lines.append(name)
                lines.append(
                    f"    {_pl_money(qty)} kg x {_pl_money(unit)} {_pl_money(line_total)} {tax}"
                )
            else:
                lines.append(name)
                lines.append(
                    f"    {int(qty)} * {_pl_money(unit)} {_pl_money(line_total)} {tax}"
                )
        else:
            lines.append(name)
            q = int(qty) if float(qty).is_integer() else qty
            lines.append(f"    {q} * {_pl_money(unit)} {_pl_money(line_total)} {tax}")

    if total:
        total_s = str(total).replace(".", ",")
        lines.append(f"Suma PLN  {total_s}")
        lines.append(f"Suma  {total_s}")

    return "\n".join(lines)


def html_to_plain(html: str) -> str:
    """Универсальный strip HTML → текст."""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    return parser.text()


def extract_pre_receipt(html: str) -> Optional[str]:
    """Достаёт текст из <pre>…</pre> (формат страницы lidl.pl/mre)."""
    m = re.search(r"<pre>(.*?)</pre>", html, re.I | re.S)
    if not m:
        return None
    # Берём pre, где есть товары или Suma PLN
    chunk = m.group(1)
    if "purchase_list" not in chunk and "Suma PLN" not in chunk and "data-art-description" not in chunk:
        # попробуем все pre
        for m2 in re.finditer(r"<pre>(.*?)</pre>", html, re.I | re.S):
            c = m2.group(1)
            if "Suma PLN" in c or "data-art-description" in c:
                chunk = c
                break
        else:
            return None
    parser = _TextExtractor()
    parser.feed(chunk)
    parser.close()
    text = parser.text()
    return text + "\n" if text else None


def ticket_to_plain(ticket: dict) -> tuple[str, str]:
    """
    Возвращает (plain_text, source_label).
    source_label: 'html_pre' | 'html_articles' | 'html_strip' | 'items_line' | 'empty'
    """
    date = ticket.get("date")
    total = ticket.get("totalAmount")

    items_line = ticket.get("itemsLine")
    if isinstance(items_line, list) and items_line:
        lines: list[str] = []
        if date:
            lines.append(str(date)[:10])
        for it in items_line:
            name = (it.get("name") or "").strip()
            if not name:
                continue
            qty_s = str(it.get("quantity") or "1")
            unit_s = str(it.get("currentUnitPrice") or "0")
            tax = str(it.get("taxGroupName") or "C")
            try:
                qty = float(qty_s.replace(",", "."))
                unit = float(unit_s.replace(",", "."))
            except ValueError:
                continue
            orig = it.get("originalAmount")
            try:
                line_total = float(str(orig).replace(",", ".")) if orig is not None else qty * unit
            except ValueError:
                line_total = qty * unit
            disc_lines = []
            for d in it.get("discounts") or []:
                try:
                    amt = abs(float(str(d.get("amount", 0)).replace(",", ".")))
                except ValueError:
                    continue
                if amt:
                    desc = (d.get("description") or "Lidl Plus").strip()
                    disc_lines.append(f"    {desc} -{_pl_money(amt)}")
            lines.append(name)
            if it.get("isWeight") or (qty != int(qty)):
                lines.append(
                    f"    {_pl_money(qty)} kg x {_pl_money(unit)} {_pl_money(abs(line_total))} {tax}"
                )
            else:
                q = int(qty) if float(qty).is_integer() else qty
                lines.append(f"    {q} * {_pl_money(unit)} {_pl_money(abs(line_total))} {tax}")
            lines.extend(disc_lines)
        if total:
            t = str(total).replace(".", ",")
            lines.append(f"Suma PLN  {t}")
            lines.append(f"Suma  {t}")
        return "\n".join(lines), "items_line"

    html = ticket.get("htmlPrintedReceipt") or ticket.get("html") or ""
    if not html and isinstance(ticket.get("raw_html"), str):
        html = ticket["raw_html"]

    if html:
        pre = extract_pre_receipt(html)
        if pre and ("Suma PLN" in pre or re.search(r"\d\s*\*\s*\d", pre)):
            return pre, "html_pre"
        built = html_articles_to_plain(
            html,
            date=str(date) if date else None,
            total=str(total) if total else None,
        )
        if built:
            return built, "html_articles"
        return html_to_plain(html), "html_strip"

    return "", "empty"


def page_html_to_plain(page_html: str) -> tuple[str, str]:
    """Конвертация сохранённой страницы purchase-detail → plain."""
    pre = extract_pre_receipt(page_html)
    if pre:
        return pre, "html_pre"
    return html_to_plain(page_html), "html_strip"
