"""
Клиент для Drebedengi SOAP API.
Используем сырой XML через requests — наиболее надёжный способ
для старого SOAP RPC/encoded стиля с Apache-типами (ns2:Map).
"""

import re
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
from xml.sax.saxutils import escape


SOAP_URL = "http://www.drebedengi.ru/soap/"
SOAP_HEADERS = {
    "Content-Type": "text/xml; charset=utf-8",
    "SOAPAction": "urn:SoapAction",
}

NS = {
    "env": "http://schemas.xmlsoap.org/soap/envelope/",
    "enc": "http://schemas.xmlsoap.org/soap/encoding/",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
    "xsd": "http://www.w3.org/2001/XMLSchema",
    "ns1": "urn:ddengi",
    "ns2": "http://xml.apache.org/xml-soap",
}

# ──────────────────────────────────────────────────────────────
# Построители XML
# ──────────────────────────────────────────────────────────────

def _envelope(method: str, inner_xml: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope
  xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/"
  xmlns:ns1="urn:ddengi"
  xmlns:xsd="http://www.w3.org/2001/XMLSchema"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xmlns:ns2="http://xml.apache.org/xml-soap"
  xmlns:SOAP-ENC="http://schemas.xmlsoap.org/soap/encoding/"
  SOAP-ENV:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <SOAP-ENV:Body>
    <ns1:{method}>
      {inner_xml}
    </ns1:{method}>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""


def _auth(api_id: str, login: str, password: str) -> str:
    return (
        f'<apiId xsi:type="xsd:string">{escape(api_id)}</apiId>'
        f'<login xsi:type="xsd:string">{escape(login)}</login>'
        f'<pass xsi:type="xsd:string">{escape(password)}</pass>'
    )


def _null(name: str) -> str:
    return f'<{name} xsi:nil="true"/>'


def _map(name: str, items: dict) -> str:
    """Строит Apache ns2:Map из словаря."""
    rows = []
    for k, v in items.items():
        if isinstance(v, bool):
            xsi_type = "xsd:boolean"
            val = "true" if v else "false"
        elif isinstance(v, int):
            xsi_type = "xsd:int"
            val = str(v)
        elif isinstance(v, float):
            xsi_type = "xsd:double"
            val = str(v)
        else:
            xsi_type = "xsd:string"
            val = escape(str(v))
        rows.append(
            f"<item>"
            f'<key xsi:type="xsd:string">{escape(k)}</key>'
            f'<value xsi:type="{xsi_type}">{val}</value>'
            f"</item>"
        )
    return f'<{name} xsi:type="ns2:Map">{"".join(rows)}</{name}>'


def _array_of_maps(name: str, records: list[dict]) -> str:
    """Строит SOAP-ENC:Array из списка словарей."""
    items = []
    for rec in records:
        inner = ""
        for k, v in rec.items():
            if isinstance(v, bool):
                xsi_type = "xsd:boolean"
                val = "true" if v else "false"
            elif isinstance(v, int):
                xsi_type = "xsd:int"
                val = str(v)
            elif isinstance(v, float):
                xsi_type = "xsd:double"
                val = str(v)
            else:
                xsi_type = "xsd:string"
                val = escape(str(v))
            inner += (
                f"<item>"
                f'<key xsi:type="xsd:string">{escape(k)}</key>'
                f'<value xsi:type="{xsi_type}">{val}</value>'
                f"</item>"
            )
        items.append(f'<item xsi:type="ns2:Map">{inner}</item>')
    count = len(records)
    return (
        f'<{name} SOAP-ENC:arrayType="xsd:anyType[{count}]" xsi:type="SOAP-ENC:Array">'
        + "".join(items)
        + f"</{name}>"
    )


def _array_of_strings(name: str, strings: list[str]) -> str:
    """Строит SOAP-ENC:Array из списка строк."""
    items = "".join(
        f'<item xsi:type="xsd:string">{escape(s)}</item>' for s in strings
    )
    count = len(strings)
    return (
        f'<{name} SOAP-ENC:arrayType="xsd:string[{count}]" xsi:type="SOAP-ENC:Array">'
        + items
        + f"</{name}>"
    )


# ──────────────────────────────────────────────────────────────
# Разбор ответов
# ──────────────────────────────────────────────────────────────

import logging as _logging
_api_logger = _logging.getLogger("drebedengi_api")


def _call(xml_body: str, log_response: bool = False) -> ET.Element:
    """Отправляет SOAP-запрос, возвращает Body ответа."""
    resp = requests.post(SOAP_URL, data=xml_body.encode("utf-8"), headers=SOAP_HEADERS, timeout=30)
    resp.raise_for_status()
    if log_response:
        _api_logger.debug("RAW SOAP response: %s", resp.text)
    root = ET.fromstring(resp.content)
    body = root.find("{http://schemas.xmlsoap.org/soap/envelope/}Body")
    # Проверяем на Fault
    fault = body.find("{http://schemas.xmlsoap.org/soap/envelope/}Fault")
    if fault is not None:
        fstring = fault.findtext("faultstring") or "Unknown SOAP Fault"
        raise RuntimeError(f"SOAP Fault: {fstring}")
    return body, resp.text


def _parse_value(el: ET.Element) -> object:
    """Рекурсивно разбирает SOAP-encoded значение."""
    # Явный nil
    is_nil = el.get("{http://www.w3.org/2001/XMLSchema-instance}nil", "")
    if is_nil in ("true", "1"):
        return None

    xsi_type = el.get("{http://www.w3.org/2001/XMLSchema-instance}type", "")

    # ns2:Map → dict
    if "Map" in xsi_type:
        result = {}
        for item in el.findall("item"):
            key_el = item.find("key")
            val_el = item.find("value")
            if key_el is not None and val_el is not None:
                result[key_el.text] = _parse_value(val_el)
        return result

    # SOAP-ENC:Array → list
    if "Array" in xsi_type:
        return [_parse_value(child) for child in el]

    # Если есть дочерние элементы — рекурсия как dict или list
    children = list(el)
    if children:
        # Если все дети называются одинаково — это массив
        names = {c.tag for c in children}
        if len(names) == 1:
            return [_parse_value(c) for c in children]
        return {c.tag: _parse_value(c) for c in children}

    # Скалярное значение
    text = el.text or ""
    if "boolean" in xsi_type:
        return text.lower() == "true"
    if "int" in xsi_type or "integer" in xsi_type:
        try:
            return int(text)
        except ValueError:
            return text
    if "double" in xsi_type or "float" in xsi_type:
        try:
            return float(text)
        except ValueError:
            return text
    return text


def _parse_response(body: ET.Element, return_tag: str) -> object:
    """Находит return-элемент в теле ответа и разбирает его."""
    # Ищем return-тег по всем дочерним (namespace может отличаться)
    for child in body:
        ret = child.find(return_tag)
        if ret is None:
            # Попробуем без namespace
            ret = child.find(f"*/{return_tag}")
        if ret is None:
            # Прямой дочерний
            for sub in child:
                if sub.tag.endswith(return_tag) or sub.tag == return_tag:
                    ret = sub
                    break
        if ret is not None:
            return _parse_value(ret)
    return None


# ──────────────────────────────────────────────────────────────
# Публичный клиент
# ──────────────────────────────────────────────────────────────

class DrebedengiClient:
    def __init__(self, api_id: str, login: str, password: str):
        self.api_id = api_id
        self.login = login
        self.password = password

    def _a(self) -> str:
        """Шорткат: строка аутентификации."""
        return _auth(self.api_id, self.login, self.password)

    # ── Служебные ──────────────────────────────────────────────

    def get_access_status(self) -> int:
        xml = _envelope("getAccessStatus", self._a())
        body, _ = _call(xml)
        return _parse_response(body, "getAccessStatusReturn")

    # ── Справочники ────────────────────────────────────────────

    def get_currency_list(self) -> list[dict]:
        xml = _envelope("getCurrencyList", self._a() + _null("idList"))
        body, _ = _call(xml)
        result = _parse_response(body, "getCurrencyListReturn")
        return result if isinstance(result, list) else ([] if result is None else [result])

    def get_category_list(self) -> list[dict]:
        xml = _envelope("getCategoryList", self._a() + _null("idList"))
        body, _ = _call(xml)
        result = _parse_response(body, "getCategoryListReturn")
        return result if isinstance(result, list) else ([] if result is None else [result])

    def get_place_list(self) -> list[dict]:
        xml = _envelope("getPlaceList", self._a() + _null("idList"))
        body, _ = _call(xml)
        result = _parse_response(body, "getPlaceListReturn")
        return result if isinstance(result, list) else ([] if result is None else [result])

    def get_source_list(self) -> list[dict]:
        """Источники дохода."""
        xml = _envelope("getSourceList", self._a() + _null("idList"))
        body, _ = _call(xml)
        result = _parse_response(body, "getSourceListReturn")
        return result if isinstance(result, list) else ([] if result is None else [result])

    def get_tag_list(self) -> list[dict]:
        xml = _envelope("getTagList", self._a() + _null("idList"))
        body, _ = _call(xml)
        result = _parse_response(body, "getTagListReturn")
        return result if isinstance(result, list) else ([] if result is None else [result])

    # ── Транзакции ─────────────────────────────────────────────

    def get_record_list(self, period: int = 8, what: int = 6) -> list[dict]:
        """
        period: 1=этот месяц, 2=прошлый, 7=сегодня, 8=последние 20
        what: 2=доходы, 3=расходы, 4=переводы, 6=все
        """
        params = {
            "is_report": False,
            "is_show_duty": True,
            "r_period": period,
            "r_how": 1,
            "r_what": what,
            "r_currency": 0,
            "r_is_place": 0,
            "r_is_tag": 0,
        }
        inner = self._a() + _map("params", params) + _null("idList")
        xml = _envelope("getRecordList", inner)
        body, _ = _call(xml)
        result = _parse_response(body, "getRecordListReturn")
        if result is None or result == "" or result == []:
            return []
        raw = result if isinstance(result, list) else [result]
        return [r for r in raw if r is not None and r != ""]

    def set_record_list(self, records: list[dict]) -> list:
        """
        Добавляет/обновляет записи.
        Каждая запись — словарь с полями:
          client_id, place_id, budget_object_id,
          sum (в копейках/центах!), operation_date,
          comment, currency_id, is_duty, operation_type
        operation_type: 2=доход, 3=расход, 4=перевод, 5=обмен
        """
        inner = self._a() + _array_of_maps("list", records)
        xml = _envelope("setRecordList", inner)
        body, raw_text = _call(xml, log_response=True)
        result = _parse_response(body, "setRecordListReturn")
        if result is None:
            _api_logger.warning("setRecordList: не удалось разобрать ответ. raw=\n%s", raw_text)
        return result, raw_text

    def parse_text_data(
        self,
        texts: list[str],
        def_place_from_id: str = "",
        def_cat_id: str = "",
        def_src_id: str = "",
        def_place_to_id: str = "",
    ) -> list[dict]:
        """
        Отправляет список текстовых строк — сервер сам пробует
        их разобрать как транзакции (расход/доход/сумму/комментарий).
        Полезно для quick-entry из Telegram.
        """
        def _id_field(name: str, val: str) -> str:
            """Передаём числовой ID или '0' если пусто (сервер не принимает nil/пустую строку)."""
            v = val.strip() if val else "0"
            if not v:
                v = "0"
            return f'<{name} xsi:type="xsd:string">{escape(v)}</{name}>'

        inner = (
            self._a()
            + _id_field("defPlaceFromId", def_place_from_id)
            + _id_field("defCatId", def_cat_id)
            + _id_field("defSrcId", def_src_id)
            + _id_field("defPlaceToId", def_place_to_id)
            + _array_of_strings("list", texts)
        )
        xml = _envelope("parseTextData", inner)
        body, _ = _call(xml)
        result = _parse_response(body, "parseTextDataReturn")
        return result if isinstance(result, list) else ([] if result is None else [result])


# ──────────────────────────────────────────────────────────────
# Быстрый smoke-test
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    client = DrebedengiClient("demo_api", "demo@example.com", "demo")

    print("=== Статус доступа ===")
    print(client.get_access_status())

    print("\n=== Валюты ===")
    for c in client.get_currency_list():
        print(f"  [{c.get('id')}] {c.get('name')} ({c.get('code')}) курс={c.get('course')}")

    print("\n=== Счета (места) ===")
    for p in client.get_place_list():
        print(f"  [{p.get('id')}] {p.get('name')}")

    print("\n=== Категории расходов ===")
    for cat in client.get_category_list():
        print(f"  [{cat.get('id')}] {cat.get('name')}")

    print("\n=== Последние записи (этот месяц) ===")
    records = client.get_record_list(period=1, what=6)
    if not records or records == [None]:
        print("  (нет записей или пустой демо-аккаунт)")
    else:
        for r in records[:10]:
            if not isinstance(r, dict):
                print(f"  raw: {r}")
                continue
            op = {2: "доход", 3: "расход", 4: "перевод", 5: "обмен"}.get(
                int(r.get("operation_type", 0)), "?"
            )
            amount = int(r.get("sum", 0)) / 100
            print(f"  {r.get('operation_date')} | {op} | {amount:.2f} | {r.get('comment')}")

    print("\n=== parseTextData: 'кофе 150' ===")
    # Используем реальные ID из уже полученных справочников
    all_places = client.get_place_list()
    all_cats = client.get_category_list()
    # Первый видимый счёт и первая видимая категория расхода (type=3)
    first_place_id = str(all_places[0]["id"]) if all_places else ""
    first_cat_id = next(
        (str(c["id"]) for c in all_cats if c.get("type") == "3" and c.get("is_hidden") != "1"),
        str(all_cats[0]["id"]) if all_cats else "",
    )
    parsed = client.parse_text_data(
        ["кофе 150", "такси 350р"],
        def_place_from_id=first_place_id,
        def_cat_id=first_cat_id,
    )
    for p in parsed:
        print(f"  {p}")
