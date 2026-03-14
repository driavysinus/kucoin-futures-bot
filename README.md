# KuCoin Futures Telegram Bot

Полнофункциональный бот для управления фьючерсным аккаунтом KuCoin через Telegram.

## Возможности

- ✅ Лимитные и рыночные ордера
- 🔁 Трейлинг-стоп ордера (нативный KuCoin API)
- ✂️ Автоматический порез позиции при движении цены на N%
- 📡 WebSocket мониторинг в реальном времени
- 🔔 Уведомления: открытие позиции, исполнение ордера, срабатывание трейлинг-стопа, порез
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

---

## Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Активировать бота (добавить чат в список уведомлений) |
| `/help` | Список всех команд |
| `/status` | Баланс USDT фьючерсного аккаунта |
| `/positions` | Открытые позиции |
| `/orders [SYMBOL]` | Активные ордера |
| `/price SYMBOL` | Текущая цена |

### Торговые команды

```
/open SYMBOL SIDE SIZE PRICE [LEVERAGE]
```
Лимитный ордер. Пример:
```
/open XBTUSDTM buy 10 26000 20
```
→ Long 10 контрактов BTC по цене 26000, плечо 20x

---

```
/market SYMBOL SIDE SIZE [LEVERAGE]
```
Рыночный ордер по текущей цене.

---

```
/trailing SYMBOL SIDE SIZE CALLBACK% [TRIGGER%] [CLOSE%] [LEVERAGE]
```
Трейлинг-стоп + автоматический порез позиции.

| Параметр | Описание |
|----------|----------|
| `SYMBOL` | Торговый символ, напр. `XBTUSDTM` |
| `SIDE` | `buy` (long) или `sell` (short) |
| `SIZE` | Размер в контрактах |
| `CALLBACK%` | Отступ трейлинг-стопа в % |
| `TRIGGER%` | % профита для автопореза (по умолч. 2.0%) |
| `CLOSE%` | % позиции для пореза (по умолч. 50%) |
| `LEVERAGE` | Плечо (по умолч. из `.env`) |

Пример:
```
/trailing XBTUSDTM buy 10 1.5 2.0 50 20
```
→ Открыл long, трейлинг-стоп с отступом 1.5%.
  Когда цена вырастет на 2% от точки входа — автоматически закроет 50% позиции.
  После этого триггер удваивается (4% → следующий порез).

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
```
Отмена ордера или всех ордеров по символу.

```
/leverage SYMBOL VALUE
```
Установить плечо для символа (используется в следующих ордерах).

---

## Архитектура

```
main.py              ← точка входа, asyncio.run()
telegram_bot.py      ← Telegram команды, роутинг, broadcast
order_manager.py     ← логика ордеров, автопорез, уведомления
position_monitor.py  ← WebSocket (private + public) каналы KuCoin
kucoin_client.py     ← REST API обёртка (async, с retry)
config.py            ← переменные окружения
```

### Поток данных

```
WebSocket (private) ─→ FuturesMonitor ─→ on_order_filled()
                                       ─→ on_trailing_stop_triggered()
                                       ─→ on_position_opened()

WebSocket (public)  ─→ FuturesMonitor ─→ on_price_update() ─→ автопорез
```

---

## Важные замечания

- Символы KuCoin Futures USDT-M: `XBTUSDTM`, `ETHUSDTM`, `SOLUSDTM`, etc.
- Размер позиции указывается в **контрактах**, не в USD/монетах
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
