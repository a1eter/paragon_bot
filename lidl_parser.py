"""
Парсер кассовых чеков Лидл (Польша).

Формат чека:
  - НазваниеТовара (без отступа)
  -     N * цена итог C/A/B  (с отступом)
  -    Lidl Plus kupon/voucher -X,XX  (скидка, с отступом, сразу после товара)
  - Opakowania zwrotne wydania  (заголовок секции кауции)
  -    Kaucja ...  X,X  (позиции кауции, с отступом)
  - Opakowania zwrotne przyjęcia  (заголовок секции возврата)
  -    Zwrot kaucji ...  -X,X  (строки возврата, с отступом)
  - Suma PLN  X,XX  — сумма товаров (контрольная)
  - Suma  X,XX      — итоговая сумма чека (контрольная)
  - XX 1068  nr:  XXXXX  HH:MM  — строка со временем
"""

import re
from typing import Optional


# ── Регулярные выражения ───────────────────────────────────────

_DATE_RE      = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_TIME_RE      = re.compile(r'\d+\s+\d+\s+nr:\s+\d+\s+(\d{1,2}:\d{2})')
_PRICE_RE     = re.compile(
    r'^\s+'
    r'(?:\d[\d,\.]*\s*\*\s*\d[\d,\.]+|\d[\d\.,]*\s*kg\s*x\s*\d[\d,\.]+)'
    r'\s+(-?\d[\d,\.]+)\s+[A-Z]\s*$'
)
# Однострочный формат: «Название  N * цена итог C» — всё без отступа
_PRICE_INLINE_RE = re.compile(
    r'^(.+?)\s{2,}'
    r'(?:\d[\d,\.]*\s*\*\s*\d[\d,\.]+|\d[\d\.,]*\s*kg\s*x\s*\d[\d,\.]+)'
    r'\s+(-?\d[\d,\.]+)\s+[A-Z]\s*$'
)
_DISCOUNT_RE  = re.compile(r'^\s+.*?(-[\d,\.]+)\s*$')
_SUMA_PLN_RE  = re.compile(r'^Suma PLN\s+([\d,\.]+)')
_SUMA_FIN_RE  = re.compile(r'^Suma\s+([\d,\.]+)\s*$')
_RAZEM_RE     = re.compile(r'^Razem\s+([\d,\.]+)\s*$')
_WYDANIA_RE   = re.compile(r'^Opakowania zwrotne wydania', re.IGNORECASE)
_PRZYJECIA_RE = re.compile(r'^Opakowania zwrotne przyjęcia', re.IGNORECASE)
_SKIP_RE      = re.compile(r'^(?:PTU\b|Kwota\b|Płatność\b|Opakowania zwrotne suma\b)')
_LAST_NUM_RE  = re.compile(r'(-?\d[\d,\.]*)\s*$')


def _to_float(s: str) -> float:
    """Конвертирует польский формат числа: '1.234,56' / '1234,56' / '1234.56' → float."""
    s = s.strip().replace('\xa0', '').replace(' ', '')
    # Убираем точку-разделитель тысяч (перед 3 цифрами и запятой)
    s = re.sub(r'\.(?=\d{3},)', '', s)
    s = s.replace(',', '.')
    return float(s)


def parse_lidl_receipt(raw: str) -> dict:
    """
    Парсит текст чека Лидл.

    Возвращает:
      date         — '2026-03-18' или None
      time         — '12:45' или None
      items        — список {'name', 'price', 'adjusted_price', 'is_kaucja'}
                     price         = цена после строчных скидок (kupon/voucher)
                     adjusted_price = цена после распределения zwrot
      zwrot        — общая сумма возврата залога
      suma_pln     — контрольная сумма из строки "Suma PLN"
      suma_final   — итоговая сумма из финальной строки "Suma"
      warnings     — список предупреждений о расхождениях
    """
    lines = raw.splitlines()

    date: Optional[str]  = None
    time_: Optional[str] = None
    items: list          = []   # {'name', 'price', 'adjusted_price', 'is_kaucja'}
    kaucja_total         = 0.0
    zwrot_total          = 0.0
    suma_pln: Optional[float]   = None
    suma_final: Optional[float] = None
    warnings: list       = []

    state        = 'products'   # 'products' | 'wydania' | 'przyjecia'
    pending_name = None

    for raw_line in lines:
        line     = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        # ── Не-индентированные строки ──────────────────────────
        if not line.startswith(' '):

            # Дата
            if _DATE_RE.match(stripped) and date is None:
                date = stripped
                continue

            # Секция кауции (wydania)
            if _WYDANIA_RE.match(stripped):
                state        = 'wydania'
                pending_name = None
                continue

            # Секция возврата (przyjecia)
            if _PRZYJECIA_RE.match(stripped):
                state        = 'przyjecia'
                pending_name = None
                continue

            # Пропускаемые строки
            if _SKIP_RE.match(stripped):
                continue

            # Suma PLN (контрольная, до кауции)
            m = _SUMA_PLN_RE.match(stripped)
            if m:
                suma_pln = _to_float(m.group(1))
                continue

            # Suma (финальная — последнее вхождение; в новом формате = итог НДС,
            # будет перебита строкой Razem ниже)
            m = _SUMA_FIN_RE.match(stripped)
            if m:
                suma_final = _to_float(m.group(1))
                continue

            # Razem (итого в новом формате без Suma PLN)
            m = _RAZEM_RE.match(stripped)
            if m:
                suma_final = _to_float(m.group(1))
                continue

            # Время из строки "XX 1068  nr: XXXXX  HH:MM"
            m = _TIME_RE.search(line)
            if m:
                time_ = m.group(1)
                continue

            # Однострочный формат: название + цена на одной строке без отступа
            if state == 'products':
                m = _PRICE_INLINE_RE.match(line)
                if m:
                    name  = m.group(1).strip()
                    price = abs(_to_float(m.group(2)))
                    items.append({
                        'name':           name,
                        'price':          price,
                        'adjusted_price': price,
                        'is_kaucja':      False,
                    })
                    pending_name = None
                    continue
                pending_name = stripped
            continue

        # ── Индентированные строки ─────────────────────────────

        if state == 'wydania':
            m = _LAST_NUM_RE.search(stripped)
            if m:
                val = _to_float(m.group(1))
                if val > 0:
                    kaucja_total = round(kaucja_total + val, 2)
            continue

        if state == 'przyjecia':
            m = _LAST_NUM_RE.search(stripped)
            if m:
                val = _to_float(m.group(1))
                if val < 0:
                    zwrot_total = round(zwrot_total + abs(val), 2)
            continue

        # state == 'products'

        # Строка с ценой товара
        m = _PRICE_RE.match(line)
        if m and pending_name:
            price = abs(_to_float(m.group(1)))
            items.append({
                'name':           pending_name,
                'price':          price,
                'adjusted_price': price,
                'is_kaucja':      False,
            })
            pending_name = None
            continue

        # Строка скидки (Lidl Plus kupon / voucher)
        m = _DISCOUNT_RE.match(line)
        if m and items and not items[-1]['is_kaucja']:
            discount = abs(_to_float(m.group(1)))
            items[-1]['price']          = round(items[-1]['price'] - discount, 2)
            items[-1]['adjusted_price'] = items[-1]['price']
            continue

        # Время — на случай если строка с индентом (редко)
        m = _TIME_RE.search(line)
        if m:
            time_ = m.group(1)
            continue

    # ── Добавляем кауцию как отдельную позицию ─────────────────
    if kaucja_total > 0:
        items.append({
            'name':           'Kaucja',
            'price':          kaucja_total,
            'adjusted_price': kaucja_total,
            'is_kaucja':      True,
        })

    # ── Распределяем возврат залога пропорционально ────────────
    if zwrot_total > 0:
        target = [i for i in items if not i['is_kaucja']]
        total  = sum(i['price'] for i in target)
        if total > 0:
            zwrot_kopecks = round(zwrot_total * 100)
            distributed   = 0
            for idx, item in enumerate(target):
                if idx == len(target) - 1:
                    share = zwrot_kopecks - distributed
                else:
                    share       = round(item['price'] / total * zwrot_kopecks)
                    distributed += share
                item['adjusted_price'] = round((item['price'] * 100 - share) / 100, 2)

    # ── Валидация ──────────────────────────────────────────────
    product_kopecks = round(sum(i['price'] for i in items if not i['is_kaucja']) * 100)
    if suma_pln is not None and product_kopecks != round(suma_pln * 100):
        warnings.append(f"⚠️ Сумма товаров {product_kopecks / 100:.2f} ≠ Suma PLN {suma_pln:.2f} в чеке")

    adjusted_kopecks = round(sum(i['adjusted_price'] for i in items) * 100)
    if suma_final is not None and adjusted_kopecks != round(suma_final * 100):
        warnings.append(f"⚠️ Итог {adjusted_kopecks / 100:.2f} ≠ {suma_final:.2f} в чеке")

    return {
        'date':       date,
        'time':       time_,
        'items':      items,
        'zwrot':      zwrot_total,
        'suma_pln':   suma_pln,
        'suma_final': suma_final,
        'warnings':   warnings,
    }
