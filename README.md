# KuCoin Futures Telegram Bot

Полнофункциональный бот для управления фьючерсным аккаунтом KuCoin через Telegram.

## Возможности

- ✅ Лимитные и рыночные ордера
- 🔁 Трейлинг-стоп ордера (нативный KuCoin API)
- ✂️ Автоматическая трёхуровневая система тейк-профитов с переносом стопа
- 🔔 Ценовые алерты — мониторинг уровней + автооткрытие позиции
- 📡 WebSocket мониторинг в реальном времени
- 🔔 Уведомления: открытие позиции, исполнение ордера, срабатывание стопа, порез
- 🖥️ Консольное управление (алерты, ордера, позиции)
- 🔐 Белый список Telegram пользователей

---

## Установка

```bash
git clone <repo>
cd kucoin_futures_bot
pip install -r requirements.txt
cp .env.example .env
```

---

## Настройка `.env`

```env
KUCOIN_API_KEY=...
KUCOIN_API_SECRET=...
KUCOIN_API_PASSPHRASE=...

TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USERS=123456789   # ваш Telegram user ID

DEFAULT_LEVERAGE=10
DEFAULT_PARTIAL_CLOSE_PCT=50       # % пореза по умолчанию
DEFAULT_TRAILING_STOP_PCT=1.5      # % отступ трейлинг-стопа
DEFAULT_PROFIT_TRIGGER_PCT=2.0     # % профита для автопореза
```

### Как получить KuCoin API ключи

1. Зайдите на [KuCoin](https://www.kucoin.com) → Профиль → API Management
2. Создайте API ключ с правами: **Futures Trading** (чтение + торговля)
3. **Не выдавайте права на вывод средств!**

### Как получить Telegram Bot Token

1. Напишите [@BotFather](https://t.me/BotFather) в Telegram
2. `/newbot` → придумайте название → получите токен

### Как узнать свой Telegram user ID

Напишите [@userinfobot](https://t.me/userinfobot) — он пришлёт ваш ID.

---

## Запуск

```bash
python main.py
```

После запуска доступны:
- Telegram команды (через бота)
- Консольный ввод (в терминале где запущен бот)

---

## Команды Telegram

| Команда | Описание |
|---------|----------|
| `/start` | Активировать бота |
| `/help` | Список всех команд |
| `/status` | Баланс USDT фьючерсного аккаунта |
| `/positions` | Открытые позиции |
| `/orders [SYMBOL]` | Активные ордера |
| `/price SYMBOL` | Текущая цена |
| `/atr SYMBOL` | ATR(21) — средний дневной диапазон |

### Торговые команды

```
/open SYMBOL SIDE USDT PRICE SL TRIG% LEV
```
Лимитный ордер + автологика. Пример:
```
/open TRUMPUSDTM buy 9 4.02 3.65 1 3
```
→ Long 9 USDT @ 4.02 | SL=3.65 | тейки каждые +1% | плечо 3x

---

```
/stop SYMBOL SIDE USDT PRICE SL TRIG% LEV
```
Стоп-маркет ордер на вход (пробой). Пример:
```
/stop TRUMPUSDTM buy 9 4.10 3.65 1 3
```
→ Вход LONG когда цена вырастет до 4.10

---

```
/market SYMBOL SIDE USDT [LEV]
```
Рыночный ордер по текущей цене.

---

```
/trailing SYMBOL SIDE USDT CALLBACK% ACTIVATE [TRIGGER%] [CLOSE%] [LEV]
```
Трейлинг-стоп + автоматический порез позиции.

---

```
/close SYMBOL [PCT]
```
Ручной порез позиции. Пример:
```
/close XBTUSDTM 50
```
→ Закрыть 50% текущей позиции по рынку.

---

```
/cancel ORDER_ID
/cancelall SYMBOL
/leverage SYMBOL VALUE
```

### Автологика после открытия позиции

При любом входе (`/open`, `/stop`, `/alert`) автоматически:

1. 🛑 **Стоп-лосс** на заданную цену SL
2. ✂️ **Тейк 1** — 50% позиции при движении +TRIG% от входа → стоп в безубыток (цена входа)
3. ✂️ **Тейк 2** — 50% от остатка при ещё +TRIG% → стоп на цену тейка 1
4. ✂️ **Тейк 3** — весь остаток при ещё +TRIG% → позиция закрыта полностью 🏁

---

## Ценовые алерты

Мониторинг цены в реальном времени через WebSocket. При достижении заданного уровня — автоматическое открытие позиции с полной логикой тейков/SL/безубытка.

### Через Telegram

```
/alert SYMBOL PRICE SIDE USDT [SL] [TRIG%] [LEV]
```

| Параметр | Описание | Обязательный |
|----------|----------|:------------:|
| `SYMBOL` | Торговый символ (`XBTUSDTM`, `WIFUSDTM`) | ✅ |
| `PRICE` | Цена срабатывания | ✅ |
| `SIDE` | `buy` (long) или `sell` (short) | ✅ |
| `USDT` | Размер позиции в USDT | ✅ |
| `SL` | Стоп-лосс (0 = без стопа) | — |
| `TRIG%` | Шаг тейков в % (по умолч. из `.env`) | — |
| `LEV` | Плечо (по умолч. из `.env`) | — |

Примеры:
```
/alert WIFUSDTM 0.17 sell 9 0.175 2 5
/alert XBTUSDTM 70000 buy 100 68000 2 10
```

Логика срабатывания:
- `buy` → цена **опустится до** trigger_price (вход в лонг на поддержке)
- `sell` → цена **вырастет до** trigger_price (вход в шорт на сопротивлении)

Управление:
```
/alerts              — список активных алертов
/rmalert ID          — удалить алерт
/clearalerts [SYMBOL] — удалить все алерты
```

### Экстренная остановка

```
/kill
```
Форсированная остановка бота. Все алерты сохранены на диск, активные ордера на бирже остаются. При следующем запуске алерты загрузятся автоматически.

### Аналитика

```
/atr SYMBOL
```
Расчёт ATR(21) — среднего истинного диапазона за 21 день. Показывает волатильность инструмента в абсолютных значениях и в процентах от цены. Символ в любом регистре: `/atr xmr`, `/atr SOL`, `/atr XBTUSDTM`.

### Уведомления

Бот отправляет уведомления в Telegram при:
- Срабатывании алерта
- Открытии/закрытии позиции
- Исполнении ордера
- Срабатывании стоп-лосса и тейк-профита
- Ошибках

**Важно:** при первом запуске нужно отправить боту `/start` в Telegram — это зарегистрирует чат. После этого ID чата сохраняется на диск (`chat_ids.json`), и при перезапуске бота уведомления продолжат работать автоматически.

### Через консоль

Те же возможности доступны в терминале:

```bash
add XBTUSDTM 70000 buy 100 68000 2 10
list
remove 3
clear
```

---

## Консольные команды

После запуска `python main.py` в терминале доступны:

| Команда | Описание |
|---------|----------|
| `add SYMBOL PRICE SIDE USDT [SL] [TRIG%] [LEV]` | Добавить алерт |
| `list` | Активные алерты |
| `remove ID` | Удалить алерт |
| `clear [SYMBOL]` | Удалить все алерты |
| `orders [SYMBOL]` | Активные ордера на бирже |
| `positions` | Открытые позиции |
| `cancel ORDER_ID` | Отменить ордер |
| `cancelall SYMBOL` | Отменить все ордера по символу |
| `close SYMBOL [PCT]` | Порез позиции (по умолч. 50%) |
| `price SYMBOL` | Текущая цена |
| `kill` | 🛑 Форсированная остановка бота |
| `help` | Справка |

---

## Архитектура

```
main.py              ← точка входа + ConsoleInput
telegram_bot.py      ← Telegram команды + AlertManager
order_manager.py     ← логика ордеров, тейки, SL, безубыток
alert_manager.py     ← ценовые алерты, мониторинг через WebSocket
console_input.py     ← консольный ввод (алерты + ордера)
position_monitor.py  ← WebSocket (private + public) каналы KuCoin
kucoin_client.py     ← REST API обёртка (async, с retry)
config.py            ← переменные окружения
```

### Поток данных

```
Telegram / Консоль
        ↓
  /alert или add → AlertManager хранит алерт
        ↓
  WebSocket (public) → FuturesMonitor → price_update
        ↓
  AlertManager: цена достигла уровня → place_market_with_plan()
        ↓
  KuCoin REST: маркет-ордер + Plan
        ↓
  WebSocket (private) → on_order_filled → _on_entry_filled
        ↓
  Тейк 1 (50%) + SL → безубыток → Тейк 2 (25%) → Тейк 3 (25%) → 🏁
        ↓
  Telegram: уведомления на каждом шаге
```

---

## Важные замечания

- Символы KuCoin Futures USDT-M: `XBTUSDTM`, `ETHUSDTM`, `SOLUSDTM`, etc.
- Размер позиции указывается в **USDT** — бот конвертирует в контракты автоматически
- Убедитесь что фьючерсный суб-аккаунт пополнен
- Бот работает только с USDT-маржинальными фьючерсами
- **Торгуйте на свой страх и риск**

---

## Systemd (автозапуск на Linux)

Создайте `/etc/systemd/system/kucoin-bot.service`:

```ini
[Unit]
Description=KuCoin Futures Telegram Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/path/to/kucoin_futures_bot
ExecStart=/usr/bin/python3 main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable kucoin-bot
sudo systemctl start kucoin-bot
sudo systemctl status kucoin-bot
```
