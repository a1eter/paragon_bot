# paragon_bot — Drebedengi Telegram Bot

Telegram-бот для быстрого добавления расходов из чеков (в первую очередь Lidl / Польша) в [drebedengi.ru](https://drebedengi.ru) напрямую через SOAP API.

## Возможности

- Отправь текст чека — бот разобьёт его на позиции, назначит категории по правилам и покажет превью
- Подтверди или отмени запись
- Для нераспознанных позиций предлагает выбрать категорию вручную (запоминает на будущее)
- Итоговое сообщение — сгруппировано по категориям с суммами и эмодзи
- Доступ ограничен по Telegram user ID

## Быстрый старт

```bash
git clone https://github.com/a1eter/paragon_bot.git
cd paragon_bot
pip install -r requirements.txt
cp .env.example .env    # заполнить своими данными
python bot.py
```

## Конфиг (.env)

```env
TELEGRAM_BOT_TOKEN=...          # от @BotFather
ALLOWED_USER_IDS=123456789      # Telegram user ID, узнать через /start в боте
                                # несколько: 111,222

DD_API_ID=demo_api              # API ID из drebedengi.ru → Настройки → API
DD_LOGIN=demo@example.com       # email аккаунта
DD_PASS=demo                    # пароль

DD_DEFAULT_CURRENCY=PLN         # валюта по умолчанию (ISO-код)
```

## Команды бота

| Команда | Действие |
|---|---|
| `/start` | Показывает твой Telegram ID |

Всё остальное — просто отправь текст чека.

## Структура проекта

```
paragon_bot/
├── bot.py                — Telegram-бот (python-telegram-bot v21)
├── drebedengi_api.py     — SOAP-клиент (сырой XML, без zeep/suds)
├── lidl_parser.py        — Парсер чеков Lidl (Польша)
├── rules.py              — Применение правил keyword → категория
├── rules.json.example    — Шаблон правил (скопируй в rules.json)
├── rules.json            — Твои правила (не коммитить, создаётся автоматически)
├── requirements.txt
├── .env.example          — Шаблон конфига
└── .env                  — Твой конфиг (не коммитить!)
```

## rules.json

Файл с правилами сопоставления ключевых слов с категориями. Бот дополняет его автоматически при ручном выборе категории. Можно редактировать вручную.

```json
{
  "patterns": [
    { "match": ["kawa", "koffie", "кофе"], "category_name": "Кафе, рестораны, доставка" },
    { "match": ["bułka", "chleb"], "category_name": "Продукты питания и напитки" }
  ]
}
```

---

## Drebedengi SOAP API — заметки

**Endpoint:** `http://www.drebedengi.ru/soap/`  
**Стиль:** RPC/encoded + Apache `ns2:Map`  
**Demo:** `demo_api` / `demo@example.com` / `demo`

### Ключевые методы

| Метод | Назначение |
|---|---|
| `getAccessStatus` | Проверка подключения |
| `getCurrencyList` | Валюты |
| `getCategoryList` | Категории расходов (`type=3`) |
| `getPlaceList` | Счета/кошельки |
| `getSourceList` | Источники дохода (`type=2`) |
| `getRecordList` | Транзакции с фильтрами |
| `setRecordList` | Добавить/обновить транзакции |
| `parseTextData` | Сервер парсит текст в транзакцию |

### Нюансы

- **`parseTextData`**: `defPlaceFromId`, `defCatId`, `defSrcId`, `defPlaceToId` — обязательно числа (или `"0"`), не пустые строки
- **`sum`** в копейках (× 100)
- **`operation_type`**: `2`=доход, `3`=расход, `4`=перевод, `5`=обмен
- Ответы разбираются вручную через `xml.etree.ElementTree`

## Быстрый старт

```powershell
cd S:\lidl_bot
pip install -r requirements.txt
copy .env.example .env    # заполнить своими токенами
python bot.py
```

## Конфиг (.env)

```env
TELEGRAM_BOT_TOKEN=...        # от @BotFather
ALLOWED_USER_IDS=123456789    # твой Telegram user ID (узнать у @userinfobot)

DD_API_ID=demo_api            # пока демо; после получения личного — заменить
DD_LOGIN=demo@example.com
DD_PASS=demo
```

## Формат ввода в боте

```
кофе 150              → расход 150 руб
такси 350р            → расход 350 руб
зарплата 50000 доход  → доход 50000 руб
обед 450 #еда         → расход, комментарий "обед #еда"
```

Бот показывает превью с кнопками **✅ Записать** / **❌ Отмена**.

## Команды бота

| Команда | Действие |
|---|---|
| `/start` | Приветствие и справка |
| `/status` | Проверить подключение к API |
| `/refresh` | Перезагрузить справочники (категории, счета, валюты) |
| `/last` | Последние 10 записей |
| `/balance` | Записи за текущий месяц |

## Структура проекта

```
S:\lidl_bot\
├── drebedengi_api.py   — SOAP-клиент (сырой XML без zeep/suds)
├── bot.py              — Telegram-бот (python-telegram-bot v21)
├── requirements.txt
├── .env.example        — шаблон конфига
└── .env                — твой конфиг (не коммитить!)
```

---

## Drebedengi SOAP API — заметки

**Endpoint:** `http://www.drebedengi.ru/soap/`  
**WSDL:** `http://www.drebedengi.ru/soap/dd.wsdl`  
**Стиль:** RPC/encoded + Apache `ns2:Map`  
**Demo:** `demo_api` / `demo@example.com` / `demo`

### Ключевые методы

| Метод | Назначение |
|---|---|
| `getAccessStatus` | Проверка (возвращает 1 если ок) |
| `getCurrencyList` | Валюты |
| `getCategoryList` | Категории расходов (`type=3`) |
| `getPlaceList` | Счета/кошельки |
| `getSourceList` | Источники дохода (`type=2`) |
| `getRecordList(params, idList)` | Транзакции с фильтрами |
| `setRecordList(list)` | Добавить/обновить транзакции |
| `parseTextData(...)` | Сервер парсит текст в транзакцию |

### Нюансы которые выяснили на практике

- **`parseTextData`**: параметры `defPlaceFromId`, `defCatId`, `defSrcId`, `defPlaceToId` — обязательно числа (или `"0"`). Пустая строка и `xsi:nil` дают `SoapFault`.
- **`sum`** хранится в **копейках** → умножать на 100 при записи, делить при чтении.
- **`operation_type`**: `2`=доход, `3`=расход, `4`=перевод, `5`=обмен валюты.
- **`client_id`** в `setRecordList` — временный клиентский ID для новой записи, любое уникальное число.
- Ответы содержат Apache `ns2:Map` — разбирается вручную через `xml.etree.ElementTree`, без внешних SOAP-библиотек.

### Пример `setRecordList`

```python
record = {
    "client_id": 1,                        # временный ID
    "place_id": 40030,                     # счёт (Сбербанк)
    "budget_object_id": 40001,             # категория (Еда)
    "sum": 15000,                          # 150.00 руб в копейках
    "operation_date": "2026-03-19 12:00:00",
    "comment": "кофе",
    "currency_id": 17,                     # RUB
    "is_duty": False,
    "operation_type": 3,                   # расход
}
dd.set_record_list([record])
```

### r_period для getRecordList

| Значение | Период |
|---|---|
| `1` | Этот месяц |
| `2` | Прошлый месяц |
| `7` | Сегодня |
| `8` | Последние 20 записей |
| `6` | Всё время |

---

## Как получить личный API ключ

Написать на drebedengi.ru кратко: суть приложения + email.  
Заменить в `.env`:
```env
DD_API_ID=твой_ключ
DD_LOGIN=твой@email.com
DD_PASS=твой_пароль
```

## Зависимости

```
python-telegram-bot==21.*   — Telegram Bot API
python-dotenv==1.*          — загрузка .env
requests==2.*               — HTTP для SOAP
```
