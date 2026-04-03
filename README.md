# KuCoin Futures Telegram Bot — Парадигма Фен-Шуй

Бот для управления фьючерсным аккаунтом KuCoin через Telegram. Автоматическое сопровождение позиций по парадигме Фен-Шуй — вся логика привязана к размеру стопа.

## Возможности

- ✅ Лимитные, рыночные и стоп-маркет ордера
- 🎯 Автоматическое сопровождение по парадигме Фен-Шуй
- 🔔 Ценовые алерты — мониторинг уровней + автооткрытие
- 📡 WebSocket мониторинг в реальном времени
- 📊 ATR(21) — аналитика волатильности
- 🔔 Telegram уведомления на каждом этапе
- 🖥️ Консольное управление
- 🔐 Белый список Telegram пользователей

---

## Парадигма Фен-Шуй

Вся логика сопровождения привязана к **stop size** = |цена входа - SL|.
TP рассчитывается автоматически: `entry ± 3 × stop_size`.

| Стопов пройдено | Действие | SL | TP |
|:-:|---|---|---|
| 1 | Безубыток | entry | без изменений |
| 2 | Порез 50% от начального | entry + 1×stop | +1×stop |
| 3 | Порез 50% от остатка | entry + 2×stop | +1×stop |
| 4+ | Только углубление | +1×stop каждый | +1×stop каждый |

Округление порезов всегда в большую сторону (ceil).

---

## Установка

```bash
git clone <repo>
cd kucoin_futures_bot
pip install -r requirements.txt
cp .env.example .env
```

## Настройка `.env`

```env
KUCOIN_API_KEY=...
KUCOIN_API_SECRET=...
KUCOIN_API_PASSPHRASE=...

TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USERS=123456789

DEFAULT_LEVERAGE=10
MARGIN_MODE=CROSS
```

## Запуск

```bash
python main.py
```

---

## Команды Telegram

### Информация

| Команда | Описание |
|---------|----------|
| `/status` | Баланс аккаунта |
| `/positions` | Открытые позиции |
| `/orders [SYMBOL]` | Активные ордера |
| `/price SYMBOL` | Текущая цена |
| `/atr SYMBOL` | ATR(21) + дистанция за день |
| `/alerts` | Активные алерты |

### Вход в позицию

```
/open SYMBOL SIDE USDT PRICE SL LEV
```
Лимитный ордер + автосопровождение Фен-Шуй.
```
/open XMRUSDTM buy 20 330 325 5
```
→ Long 20 USDT @ 330 | SL=325 | Stop size=5 | TP=345 | Плечо 5x

---

```
/stop SYMBOL SIDE USDT PRICE SL LEV
```
Стоп-маркет на вход (пробой уровня).
```
/stop XMRUSDTM sell 20 325 330 5
```
→ Short при падении до 325 | SL=330 | TP=310 | Плечо 5x

---

```
/market SYMBOL SIDE USDT [LEV]
```
Рыночный ордер (без автологики).

### Ценовые алерты

```
/alert SYMBOL PRICE SIDE USDT SL LEV
```
Мониторинг цены + автооткрытие + Фен-Шуй.
```
/alert XMR 330 buy 20 325 5
```
→ При достижении 330 — лонг | SL=325 | TP=345 | Плечо 5x

Направление определяется автоматически:
- Триггер ниже текущей цены → ждём падения
- Триггер выше текущей цены → ждём роста

```
/alerts              — список активных алертов
/rmalert ID          — удалить алерт
/clearalerts [SYMBOL] — удалить все алерты
```

### Аналитика

```
/atr SYMBOL
```
ATR(21) + пройденная дистанция за сегодня. Символ в любом регистре.

### Управление

```
/close SYMBOL [PCT]  — ручной порез позиции
/cancel ORDER_ID     — отмена ордера
/cancelall SYMBOL    — отмена всех ордеров
/leverage SYMBOL VAL — установить плечо
/kill                — экстренная остановка бота
```

---

## Консольные команды

| Команда | Описание |
|---------|----------|
| `add SYMBOL PRICE SIDE USDT SL LEV` | Добавить алерт |
| `list` | Активные алерты |
| `remove ID` | Удалить алерт |
| `clear` | Удалить все алерты |
| `orders [SYMBOL]` | Ордера на бирже |
| `positions` | Открытые позиции |
| `cancel ORDER_ID` | Отменить ордер |
| `cancelall SYMBOL` | Отменить все ордера |
| `close SYMBOL [PCT]` | Порез позиции |
| `price SYMBOL` | Текущая цена |
| `kill` | Остановка бота |

---

## Архитектура

```
main.py              ← точка входа + ConsoleInput
telegram_bot.py      ← Telegram команды
order_manager.py     ← Фен-Шуй: stop_size логика, порезы, углубления
alert_manager.py     ← ценовые алерты + WebSocket мониторинг
console_input.py     ← консольный ввод
position_monitor.py  ← WebSocket (private + public) KuCoin
kucoin_client.py     ← REST API + klines для ATR
config.py            ← переменные окружения
```

### Поток данных

```
/alert или /open → OrderManager создаёт Plan
        ↓
WebSocket (public) → price_update → OrderManager.on_price_update()
        ↓
  Считает пройденные стопы:
    1 stop → безубыток
    2 стопа → порез 50% + углубление SL/TP
    3 стопа → порез 50% остатка + углубление
    4+ → только углубление
        ↓
  Отмена старых SL/TP → выставление новых на бирже
        ↓
  Telegram: уведомление на каждом этапе
```

---

## Важные замечания

- Размер позиции в **USDT** — бот конвертирует в контракты
- **TP = entry ± 3 × stop_size** — рассчитывается автоматически
- Порезы округляются в большую сторону (ceil)
- Алерты сохраняются в `alerts.json` — переживают перезапуск
- Chat ID сохраняется в `chat_ids.json` — уведомления работают после рестарта
- **Торгуйте на свой страх и риск**

---

## Systemd (автозапуск на Linux)

```ini
[Unit]
Description=KuCoin Futures Telegram Bot
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/kucoin-futures-bot
ExecStart=/usr/bin/python3 main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable kucoin-bot
sudo systemctl start kucoin-bot
```
