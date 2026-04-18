# KuCoin Futures Telegram Bot — Парадигма Фен-Шуй

Бот для управления фьючерсным аккаунтом KuCoin через Telegram. Автоматическое сопровождение позиций по парадигме Фен-Шуй — вся логика привязана к размеру стопа.

## Возможности

- ✅ Лимитные, рыночные и стоп-маркет ордера
- 🎯 Автоматическое сопровождение по парадигме Фен-Шуй
- 🧠 **SL/TP виртуальные** — не выставляются на бирже, мониторинг через WebSocket, закрытие маркетом при касании
- 🔁 Авто-верификация закрытия (до 3 повторов) + алёрт если позиция осталась
- 🔔 Ценовые алерты с автооткрытием + уведомления без сделки (`/notify`)
- 📡 WebSocket private+public в реальном времени
- 📊 ATR(21) — аналитика волатильности и дистанции за день
- 💾 Персистентность: `alerts.json`, `chat_ids.json` — переживают рестарт
- 🖥️ Консольное управление параллельно с Telegram
- 🔐 Белый список Telegram пользователей

---

## Парадигма Фен-Шуй

Вся логика сопровождения привязана к **stop size** = |фактическая цена входа - SL|.
После исполнения входа `stop_size` пересчитывается по реальной цене из позиции (`avgEntryPrice`).
TP рассчитывается автоматически: `entry ± 3 × stop_size`.

| Стопов пройдено | Порез | SL | TP |
|:-:|---|---|---|
| 1 | 50% от начального объёма | initial (не двигаем) | без изменений |
| 2 | 50% от остатка | entry ± 1×stop (старт трейлинга) | без изменений |
| 3+ | — | динамический трейлинг `price ∓ stop_size` (только в плюс) | +1×stop на уровень |

Порезы округляются вверх (`ceil`). Перед каждым уровнем `remaining` синхронизируется с реальной позицией на бирже. Трейлинг-SL активен после 2-го пореза и двигается на каждом тике WebSocket.

### Как работают SL/TP

SL и TP **не выставляются на бирже**. `OrderManager` подписан на `price_update` WebSocket и сам проверяет касание уровней. При срабатывании — маркет-ордер `reduceOnly`, затем верификация (`get_position`) с до 3 повторов закрытия; если позиция всё ещё жива — критический алёрт в Telegram.

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
/alert SYMBOL PRICE SIDE USDT SL [LEV]
```
Мониторинг цены + автооткрытие **по рынку** с полным сопровождением Фен-Шуй. `LEV` опционален — если не задан, берётся `DEFAULT_LEVERAGE`.
```
/alert XMR 330 buy 20 325 5
```
→ При достижении 330 — лонг маркетом | SL=325 | TP=345 | Плечо 5x

Направление определяется автоматически от текущей цены:
- Триггер ниже текущей цены → `direction=down`
- Триггер выше текущей цены → `direction=up`

```
/notify SYMBOL PRICE     — уведомление при достижении цены (без сделки)
/alerts                  — список активных алертов
/rmalert ID              — удалить алерт
/clearalerts [SYMBOL]    — удалить все алерты
```

Алерты сохраняются в `alerts.json` и восстанавливаются при рестарте.

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
/open | /stop | /alert → OrderManager создаёт Plan (stop_size, current_sl, current_tp)
        ↓
WebSocket private → on_order_filled → _on_entry_filled
  (пересчёт stop_size и TP по avgEntryPrice из позиции)
        ↓
WebSocket public → price_update → OrderManager.on_price_update()
        ├── цена пробила current_sl → _execute_sl (маркет + verify до 3 повторов)
        ├── цена пробила current_tp → _execute_tp (маркет + verify)
        └── пройдено N стопов в прибыль → _handle_stop_level(N):
              1 → _level_1_breakeven     (SL = entry)
              2 → _level_2_first_cut     (ceil(contracts/2), SL +1 stop, TP +1 stop)
              3 → _level_3_second_cut    (ceil(remaining/2), SL +2 stop, TP +1 stop)
              4+ → _level_n_trail        (углубление SL/TP по +1 stop)
        ↓
  Виртуальные SL/TP обновляются в памяти (на биржу НЕ отправляются)
        ↓
  Telegram: уведомление на каждом этапе
```

---

## Важные замечания

- Размер позиции задаётся в **USDT**, бот сам конвертирует в контракты через `usdt_to_contracts` (учитывает `multiplier`).
- **TP = entry ± 3 × stop_size** рассчитывается автоматически; после исполнения входа пересчитывается по реальной средней цене позиции.
- **SL и TP не существуют на бирже** — только в памяти бота. Если процесс упал, сопровождение прерывается до рестарта (плановый вход + план будут восстановлены только для алертов из `alerts.json`; активные планы позиций — нет).
- Порезы округляются вверх (`ceil`), минимум 1 контракт.
- Алерты сохраняются в `alerts.json`, chat ID — в `chat_ids.json`, оба файла переживают рестарт.
- Параллельно с Telegram работает консольный ввод (`console_input.py`) — можно управлять прямо из терминала, где запущен `main.py`.
- **Торгуйте на свой страх и риск.**

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
