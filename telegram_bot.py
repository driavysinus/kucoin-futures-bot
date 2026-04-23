"""
telegram_bot.py
Telegram bot interface for KuCoin Futures trading.
Все размеры позиций задаются в USDT — бот автоматически конвертирует в контракты.

Commands:
  /start                                    — приветствие
  /help                                     — список команд
  /status                                   — баланс аккаунта
  /positions                                — открытые позиции
  /orders [SYMBOL]                          — активные ордера
  /open SYMBOL SIDE USDT PRICE [LEV]        — лимитный ордер
  /market SYMBOL SIDE USDT [LEV]            — рыночный ордер
  /trailing SYMBOL SIDE USDT CALLBACK [TRIGGER%] [CLOSE%] [LEV]
  /close SYMBOL [PCT]                       — порез позиции
  /cancel ORDER_ID                          — отмена ордера
  /cancelall SYMBOL                         — отмена всех ордеров по символу
  /leverage SYMBOL VALUE                    — установить плечо для символа
  /price SYMBOL                             — текущая цена
  /alert SYMBOL PRICE SIDE USDT [SL] [TRIG%] [LEV] — ценовой алерт
  /alerts                                   — список активных алертов
  /rmalert ID                               — удалить алерт
  /clearalerts [SYMBOL]                     — удалить все алерты
"""

import asyncio
import json
import os
import sys
from functools import wraps
from loguru import logger

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters
)

import config
from kucoin_client import KuCoinFuturesClient
from order_manager import OrderManager
from position_monitor import FuturesMonitor
from alert_manager import AlertManager


# ── Auth decorator ────────────────────────────────────────────────────────────
def restricted(func):
    @wraps(func)
    async def wrapper(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if config.TELEGRAM_ALLOWED_USERS and uid not in config.TELEGRAM_ALLOWED_USERS:
            await update.message.reply_text("⛔ Доступ запрещён.")
            logger.warning(f"Unauthorized access attempt from {uid}")
            return
        return await func(self, update, ctx)
    return wrapper


# ── Helper ────────────────────────────────────────────────────────────────────
def _parse(args: list, idx: int, cast=str, default=None):
    try:
        val = args[idx]
        if cast in (float, int):
            val = val.replace(",", ".")
        return cast(val)
    except (IndexError, ValueError, TypeError):
        return default

def _float(s: str) -> float:
    """Парсит float, принимая и точку и запятую как разделитель."""
    return float(str(s).replace(",", "."))


CHAT_IDS_FILE = "chat_ids.json"


class TradingBot:
    def __init__(self):
        self.client  = KuCoinFuturesClient()
        self.monitor = FuturesMonitor(self.client)
        self.manager = OrderManager(self.client, notify=self._broadcast)
        self.alert_manager = AlertManager(
            self.manager, self.monitor, notify=self._broadcast
        )
        self._chat_ids: set[int] = set()
        self._app: Application = None
        self._load_chat_ids()

    # ── Chat IDs persistence ──────────────────────────────────────────────────
    def _save_chat_ids(self):
        try:
            with open(CHAT_IDS_FILE, "w") as f:
                json.dump(list(self._chat_ids), f)
        except Exception as e:
            logger.error(f"Failed to save chat_ids: {e}")

    def _load_chat_ids(self):
        if not os.path.exists(CHAT_IDS_FILE):
            return
        try:
            with open(CHAT_IDS_FILE, "r") as f:
                ids = json.load(f)
            self._chat_ids = set(ids)
            if self._chat_ids:
                logger.info(f"Loaded {len(self._chat_ids)} chat IDs from {CHAT_IDS_FILE}")
        except Exception as e:
            logger.error(f"Failed to load chat_ids: {e}")

    def _register_chat(self, update):
        """Регистрирует чат и сохраняет на диск."""
        cid = update.effective_chat.id
        if cid not in self._chat_ids:
            self._chat_ids.add(cid)
            self._save_chat_ids()

    # ── Broadcast ─────────────────────────────────────────────────────────────
    async def _broadcast(self, text: str):
        if not self._app:
            logger.warning("Broadcast skipped: _app not initialized")
            return
        if not self._chat_ids:
            logger.warning("Broadcast skipped: no chat IDs (send /start to bot first)")
            return
        for cid in list(self._chat_ids):
            try:
                await self._app.bot.send_message(
                    chat_id=cid, text=text, parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.error(f"Broadcast error to {cid}: {e}")

    # ── Commands ──────────────────────────────────────────────────────────────
    @restricted
    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self._register_chat(update)
        await update.message.reply_text(
            "🤖 *KuCoin Futures Bot активирован*\n\n"
            "Введите /help для списка команд.",
            parse_mode=ParseMode.MARKDOWN
        )

    @restricted
    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "*📖 Команды бота — Парадигма Фен-Шуй*\n\n"
            "*Информация:*\n"
            "`/status` — баланс аккаунта\n"
            "`/positions` — открытые позиции\n"
            "`/orders [SYMBOL]` — активные ордера\n"
            "`/price SYMBOL` — текущая цена\n"
            "`/atr SYMBOL` — ATR(21) + дистанция за день\n"
            "`/alerts` — активные ценовые алерты\n\n"

            "*Вход в позицию:*\n"
            "`/open SYMBOL SIDE USDT PRICE SL LEV`\n"
            "  Лимитный ордер. TP рассчитывается автоматически.\n"
            "  Пример: `/open XMRUSDTM buy 20 330 325 5`\n"
            "  → Long 20 USDT @ 330 | SL=325 | Плечо 5x\n"
            "  → Stop size=5 | TP=330+15=345\n\n"

            "`/stop SYMBOL SIDE USDT PRICE SL LEV`\n"
            "  Стоп-маркет на вход (пробой уровня).\n"
            "  Пример: `/stop XMRUSDTM sell 20 325 330 5`\n"
            "  → Short при падении до 325 | SL=330 | TP=310\n\n"

            "`/alert SYMBOL PRICE SIDE USDT SL LEV`\n"
            "  Ценовой алерт + автооткрытие по рынку.\n"
            "  Пример: `/alert XMR 330 buy 20 325 5`\n\n"

            "`/market SYMBOL SIDE USDT [LEV]`\n"
            "  Рыночный ордер (без автологики).\n\n"

            "*Парадигма Фен-Шуй (автоматически):*\n"
            "  Всё считается от `stop size` = |вход - SL|\n"
            "  TP = вход ± 3 × stop size\n\n"
            "  `1 stop` → SL на вход (безубыток)\n"
            "  `2 стопа` → порез 50%, SL +1 stop, TP +1 stop\n"
            "  `3 стопа` → порез 50% остатка, SL +1 stop, TP +1 stop\n"
            "  `4+ стопов` → только углубление SL и TP\n\n"

            "*Управление:*\n"
            "`/close SYMBOL [PCT]` — ручной порез\n"
            "`/cancel ORDER_ID` — отмена ордера\n"
            "`/cancelall SYMBOL` — отмена всех ордеров\n"
            "`/leverage SYMBOL VALUE` — плечо\n\n"

            "*Алерты:*\n"
            "`/notify SYMBOL PRICE` — уведомление при цене (без сделки)\n"
            "`/alerts` — список\n"
            "`/rmalert ID` — удалить\n"
            "`/clearalerts` — удалить все\n\n"

            "*Экстренное:*\n"
            "`/kill` — остановка бота",
            parse_mode=ParseMode.MARKDOWN
        )

    @restricted
    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self._register_chat(update)
        try:
            acc = await self.client.get_account_overview("USDT")
            await update.message.reply_text(
                f"💼 *Баланс фьючерсного аккаунта*\n\n"
                f"Доступно:  `{float(acc.get('availableBalance', 0)):.4f} USDT`\n"
                f"Маржа:     `{float(acc.get('positionMargin', 0)):.4f} USDT`\n"
                f"Нереал. PnL: `{float(acc.get('unrealisedPNL', 0)):.4f} USDT`\n"
                f"Общий баланс: `{float(acc.get('accountEquity', 0)):.4f} USDT`",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

    @restricted
    async def cmd_positions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self._register_chat(update)
        try:
            positions = await self.client.get_positions()
            active    = [p for p in positions if float(p.get("currentQty", 0)) != 0]
            if not active:
                await update.message.reply_text("📭 Нет открытых позиций")
                return
            lines = ["*📊 Открытые позиции:*\n"]
            for p in active:
                qty   = float(p.get("currentQty", 0))
                side  = "LONG 📈" if qty > 0 else "SHORT 📉"
                pnl   = float(p.get("unrealisedPnl", 0))
                pnl_e = "🟢" if pnl >= 0 else "🔴"
                lines.append(
                    f"*{p.get('symbol')}* | {side}\n"
                    f"  Объём: `{abs(qty)}` | Вход: `{p.get('avgEntryPrice', 'N/A')}`\n"
                    f"  Цена ликв.: `{p.get('liquidationPrice', 'N/A')}`\n"
                    f"  PnL: {pnl_e} `{pnl:.4f} USDT`\n"
                )
            await update.message.reply_text(
                "\n".join(lines), parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

    @restricted
    async def cmd_orders(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self._register_chat(update)
        symbol = _parse(ctx.args, 0)
        try:
            orders = await self.client.get_open_orders(symbol)
            if not orders:
                await update.message.reply_text("📭 Нет активных ордеров")
                return
            lines = [f"*📋 Активные ордера{' (' + symbol + ')' if symbol else ''}:*\n"]
            for o in orders[:15]:
                otype      = o.get("type", "")
                is_stop    = bool(o.get("stop"))
                is_trail   = "trailing" in str(o.get("trailingStop", ""))
                reduce     = o.get("reduceOnly", False)

                if is_trail:
                    label = "🔁 Трейлинг"
                elif is_stop and reduce:
                    label = "🎯 Стоп-закрытие"
                elif is_stop:
                    label = "🎯 Стоп-вход"
                else:
                    label = "📋 Лимит"

                stop_price = o.get("stopPrice", "")
                price      = o.get("price", "0")
                show_price = stop_price if is_stop else price

                oid = o.get("id", o.get("orderId", ""))
                lines.append(
                    f"{label} `{str(oid)[:12]}…`\n"
                    f"  {o.get('symbol')} | {'BUY' if o.get('side')=='buy' else 'SELL'} "
                    f"| Размер: `{o.get('size')}` | Цена: `{show_price}`\n"
                )
            await update.message.reply_text(
                "\n".join(lines), parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

    @restricted
    async def cmd_open(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """
        /open SYMBOL SIDE USDT PRICE SL [LEV]
        Лимитный ордер + автологика Фен-Шуй.
        """
        self._register_chat(update)
        args = ctx.args
        if len(args) < 5:
            await update.message.reply_text(
                "❌ Использование:\n"
                "`/open SYMBOL SIDE USDT PRICE SL [LEV]`\n\n"
                "Пример:\n"
                "`/open XMRUSDTM buy 20 330 325 5`\n"
                "  → Long 20 USDT @ 330 | SL=325 | TP=345\n"
                "  → Stop size=5, плечо 5x\n\n"
                "`/open ETHUSDTM sell 50 3800 3850 3`\n"
                "  → Short @ 3800 | SL=3850 | TP=3650",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        symbol = args[0].upper()
        side   = args[1].lower()
        if side not in ("buy", "sell"):
            await update.message.reply_text("❌ SIDE должен быть `buy` или `sell`",
                                             parse_mode=ParseMode.MARKDOWN)
            return
        try:
            usdt_amount = _float(args[2])
            price       = _float(args[3])
            sl_price    = _float(args[4])
            lev         = _parse(args, 5, int, config.DEFAULT_LEVERAGE)

            self.monitor.subscribe_ticker(symbol)
            self.manager.set_leverage(symbol, lev)

            await self.manager.place_limit_order(
                symbol, side, usdt_amount, price,
                sl_price=sl_price, trim_pct=0, leverage=lev,
            )
        except Exception as e:
            logger.error(f"cmd_open error: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка: {e}")

    @restricted
    async def cmd_stop_entry(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """
        /stop SYMBOL SIDE USDT PRICE SL [LEV]
        Стоп-маркет на вход + автологика Фен-Шуй.
        """
        self._register_chat(update)
        args = ctx.args
        if len(args) < 5:
            await update.message.reply_text(
                "❌ Использование:\n"
                "`/stop SYMBOL SIDE USDT PRICE SL [LEV]`\n\n"
                "Пример:\n"
                "`/stop XMRUSDTM buy 20 340 335 5`\n"
                "  → Вход LONG при росте до 340 | SL=335 | TP=355\n\n"
                "`/stop XMRUSDTM sell 20 325 330 5`\n"
                "  → Вход SHORT при падении до 325 | SL=330 | TP=310",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        symbol = args[0].upper()
        side   = args[1].lower()
        if side not in ("buy", "sell"):
            await update.message.reply_text("❌ SIDE должен быть `buy` или `sell`",
                                             parse_mode=ParseMode.MARKDOWN)
            return
        try:
            usdt_amount = _float(args[2])
            price       = _float(args[3])
            sl_price    = _float(args[4])
            lev         = _parse(args, 5, int, config.DEFAULT_LEVERAGE)

            self.manager.set_leverage(symbol, lev)
            self.monitor.subscribe_ticker(symbol)

            await self.manager.place_stop_entry(
                symbol, side, usdt_amount, price,
                sl_price=sl_price, trim_pct=0, leverage=lev,
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")


    @restricted
    async def cmd_market(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Usage: /market SYMBOL SIDE USDT [LEVERAGE]"""
        self._register_chat(update)
        args = ctx.args
        if len(args) < 3:
            await update.message.reply_text(
                "❌ Использование: `/market SYMBOL SIDE USDT [LEV]`\n"
                "Пример: `/market XBTUSDTM buy 500 20`",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        symbol = args[0].upper()
        side   = args[1].lower()
        try:
            usdt_amount = _float(args[2])
            lev         = _parse(args, 3, int, config.DEFAULT_LEVERAGE)
            self.manager.set_leverage(symbol, lev)
            self.monitor.subscribe_ticker(symbol)
            contracts, price, multiplier = await self.client.usdt_to_contracts(
                symbol, usdt_amount
            )
            actual_usdt = contracts * price * multiplier
            oid = await self.manager.place_market_order(
                symbol, side, usdt_amount, lev
            )
            await update.message.reply_text(
                f"✅ *Рыночный ордер отправлен*\n"
                f"Символ: `{symbol}`\n"
                f"Направление: `{'LONG 📈' if side=='buy' else 'SHORT 📉'}`\n"
                f"Размер: `{actual_usdt:.2f} USDT` → `{contracts}` контрактов\n"
                f"ID: `{oid}`",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

    @restricted
    async def cmd_trailing(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """
        Usage: /trailing SYMBOL SIDE USDT CALLBACK ACTIVATE [TRIGGER%] [CLOSE%] [LEV]
        ACTIVATE — цена активации трейлинг-стопа (обязательный параметр)
        """
        self._register_chat(update)
        args = ctx.args
        if len(args) < 5:
            await update.message.reply_text(
                "❌ Использование:\n"
                "`/trailing SYMBOL SIDE USDT CALLBACK% ACTIVATE [TRIGGER%] [CLOSE%] [LEV]`\n\n"
                "Параметры:\n"
                "  `USDT` — размер позиции в USDT\n"
                "  `CALLBACK%` — отступ трейлинг-стопа в %\n"
                "  `ACTIVATE` — *цена активации* стопа\n"
                f"  `TRIGGER%` — % профита для автопореза (по умолч. {config.DEFAULT_PROFIT_TRIGGER_PCT}%)\n"
                f"  `CLOSE%` — % позиции для пореза (по умолч. {config.DEFAULT_PARTIAL_CLOSE_PCT}%)\n"
                "  `LEV` — плечо\n\n"
                "Пример:\n"
                "`/trailing SKYUSDTM buy 9 10 0.45 10 50 3`\n"
                "  → Активация при цене `0.45`\n"
                "  → Callback `10%`, автопорез `50%` при `+10%`\n"
                "  → Плечо `3x`",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        symbol = args[0].upper()
        side   = args[1].lower()
        if side not in ("buy", "sell"):
            await update.message.reply_text("❌ SIDE должен быть `buy` или `sell`",
                                             parse_mode=ParseMode.MARKDOWN)
            return
        try:
            usdt_amount    = _float(args[2])
            callback_rate  = _float(args[3])
            activate_price = _float(args[4])
            trigger        = _parse(args, 5, float, config.DEFAULT_PROFIT_TRIGGER_PCT)
            close_pct      = _parse(args, 6, float, config.DEFAULT_PARTIAL_CLOSE_PCT)
            lev            = _parse(args, 7, int,   config.DEFAULT_LEVERAGE)

            if activate_price <= 0:
                await update.message.reply_text("❌ Цена активации должна быть больше 0",
                                                 parse_mode=ParseMode.MARKDOWN)
                return

            self.manager.set_leverage(symbol, lev)
            self.monitor.subscribe_ticker(symbol)

            await self.manager.place_trailing_stop(
                symbol, side, usdt_amount, callback_rate,
                activate_price=activate_price,
                leverage=lev, profit_trigger=trigger, partial_close_pct=close_pct
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

    @restricted
    async def cmd_close(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Usage: /close SYMBOL [PCT]"""
        self._register_chat(update)
        args = ctx.args
        if not args:
            await update.message.reply_text(
                "❌ Использование: `/close SYMBOL [PCT]`\n"
                "Пример: `/close XBTUSDTM 50`",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        symbol    = args[0].upper()
        close_pct = _parse(args, 1, float, config.DEFAULT_PARTIAL_CLOSE_PCT)
        try:
            await self.manager.partial_close(symbol, close_pct, "Ручной порез")
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

    @restricted
    async def cmd_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Usage: /cancel ORDER_ID"""
        self._register_chat(update)
        if not ctx.args:
            await update.message.reply_text("❌ Использование: `/cancel ORDER_ID`",
                                             parse_mode=ParseMode.MARKDOWN)
            return
        await self.manager.cancel_order(ctx.args[0])

    @restricted
    async def cmd_cancelall(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Usage: /cancelall SYMBOL"""
        self._register_chat(update)
        if not ctx.args:
            await update.message.reply_text("❌ Использование: `/cancelall SYMBOL`",
                                             parse_mode=ParseMode.MARKDOWN)
            return
        await self.manager.cancel_all(ctx.args[0].upper())

    @restricted
    async def cmd_leverage(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Usage: /leverage SYMBOL VALUE"""
        self._register_chat(update)
        args = ctx.args
        if len(args) < 2:
            await update.message.reply_text(
                "❌ Использование: `/leverage SYMBOL VALUE`\n"
                "Пример: `/leverage XBTUSDTM 20`",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        symbol = args[0].upper()
        lev    = int(args[1])
        self.manager.set_leverage(symbol, lev)
        await update.message.reply_text(
            f"✅ Плечо для `{symbol}` установлено: `{lev}x`",
            parse_mode=ParseMode.MARKDOWN
        )

    @restricted
    async def cmd_price(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Usage: /price SYMBOL"""
        self._register_chat(update)
        if not ctx.args:
            await update.message.reply_text("❌ Использование: `/price SYMBOL`",
                                             parse_mode=ParseMode.MARKDOWN)
            return
        symbol = ctx.args[0].upper()
        try:
            # Try WebSocket cache first (fast)
            price = self.monitor.get_price(symbol)
            if not price:
                price = await self.client.get_mark_price(symbol)
            await update.message.reply_text(
                f"💹 `{symbol}`: `{price}`",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

    # ── Alerts (Telegram) ─────────────────────────────────────────────────────
    @restricted
    async def cmd_alert(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """
        /alert SYMBOL PRICE SIDE USDT SL [LEV]
        Ценовой алерт + автооткрытие + Фен-Шуй.
        """
        self._register_chat(update)
        args = ctx.args
        if len(args) < 5:
            await update.message.reply_text(
                "❌ Использование:\n"
                "`/alert SYMBOL PRICE SIDE USDT SL [LEV]`\n\n"
                "Параметры:\n"
                "  `SYMBOL` — торговая пара\n"
                "  `PRICE` — цена срабатывания\n"
                "  `SIDE` — `buy` (long) или `sell` (short)\n"
                "  `USDT` — размер позиции\n"
                "  `SL` — стоп-лосс\n"
                "  `LEV` — плечо\n\n"
                "Примеры:\n"
                "`/alert XMR 330 buy 20 325 5`\n"
                "  → Лонг при 330 | SL=325 | TP=345\n\n"
                "`/alert XMR 340 sell 20 345 5`\n"
                "  → Шорт при 340 | SL=345 | TP=325",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        try:
            symbol      = args[0].upper()
            if symbol.endswith("USDTM"):
                pass
            elif symbol.endswith("USDT"):
                symbol += "M"
            else:
                symbol += "USDTM"

            price       = _float(args[1])
            side        = args[2].lower()
            usdt_amount = _float(args[3])

            if side not in ("buy", "sell"):
                await update.message.reply_text("❌ SIDE должен быть `buy` или `sell`",
                                                 parse_mode=ParseMode.MARKDOWN)
                return

            sl_price = _float(args[4]) if len(args) > 4 else 0.0
            leverage = _parse(args, 5, int, None)

            alert = await self.alert_manager.add_alert(
                symbol=symbol,
                trigger_price=price,
                side=side,
                usdt_amount=usdt_amount,
                sl_price=sl_price,
                trim_pct=0,
                leverage=leverage,
            )

            direction = "📉 ждём падения до" if alert.direction == "down" else "📈 ждём роста до"
            stop_size = abs(price - sl_price) if sl_price > 0 else 0
            if stop_size > 0:
                if side == "buy":
                    tp_est = round(price + 3 * stop_size, 8)
                else:
                    tp_est = round(price - 3 * stop_size, 8)
                tp_str = f"\nTP (расчётный): `{tp_est}` (3x stop)"
            else:
                tp_str = ""

            await update.message.reply_text(
                f"✅ *Алерт #{alert.id} — Фен-Шуй*\n"
                f"Символ: `{alert.symbol}`\n"
                f"Направление: `{'LONG 📈' if side=='buy' else 'SHORT 📉'}`\n"
                f"Триггер: {direction} `{price}`\n"
                f"Объём: `{usdt_amount} USDT` | Плечо: `{alert.leverage}x`\n"
                f"SL: `{sl_price}` | Stop size: `{stop_size}`{tp_str}\n\n"
                f"_Мониторинг запущен. При достижении — автовход + сопровождение._\n"
                f"_Отмена:_ `/rmalert {alert.id}`",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

    @restricted
    async def cmd_notify(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """
        /notify SYMBOL PRICE — уведомление при достижении цены (без сделки).
        """
        self._register_chat(update)
        args = ctx.args
        if len(args) < 2:
            await update.message.reply_text(
                "❌ Использование:\n"
                "`/notify SYMBOL PRICE`\n\n"
                "Пример:\n"
                "`/notify XMR 330` — уведомить когда XMR достигнет 330",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        try:
            symbol = args[0].upper()
            if symbol.endswith("USDTM"):
                pass
            elif symbol.endswith("USDT"):
                symbol += "M"
            else:
                symbol += "USDTM"

            price = _float(args[1])

            alert = await self.alert_manager.add_notify_alert(
                symbol=symbol,
                trigger_price=price,
            )

            direction = "📉 ждём падения до" if alert.direction == "down" else "📈 ждём роста до"
            await update.message.reply_text(
                f"✅ *Уведомление #{alert.id}*\n"
                f"Символ: `{alert.symbol}`\n"
                f"Триггер: {direction} `{price}`\n\n"
                f"_При достижении цены — уведомление в Telegram (без сделки)._\n"
                f"_Отмена:_ `/rmalert {alert.id}`",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

    @restricted
    async def cmd_rmalert(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Usage: /rmalert ID — удалить алерт"""
        self._register_chat(update)
        if not ctx.args:
            await update.message.reply_text("❌ Использование: `/rmalert ID`",
                                             parse_mode=ParseMode.MARKDOWN)
            return
        try:
            alert_id = int(ctx.args[0])
            removed = self.alert_manager.remove_alert(alert_id)
            if removed:
                await update.message.reply_text(
                    f"🗑 Алерт #{alert_id} удалён\n"
                    f"`{removed.symbol}` {removed.side} @ `{removed.trigger_price}`",
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await update.message.reply_text(f"❌ Алерт #{alert_id} не найден")
        except ValueError:
            await update.message.reply_text("❌ ID должен быть числом")

    @restricted
    async def cmd_clearalerts(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Usage: /clearalerts [SYMBOL] — удалить все алерты"""
        self._register_chat(update)
        symbol = ctx.args[0].upper() if ctx.args else None
        before = len(self.alert_manager.list_alerts())
        self.alert_manager.clear_alerts(symbol)
        after = len(self.alert_manager.list_alerts())
        removed = before - after
        suffix = f" по `{symbol}`" if symbol else ""
        await update.message.reply_text(
            f"🗑 Удалено алертов: {removed}{suffix}",
            parse_mode=ParseMode.MARKDOWN
        )

    @restricted
    async def cmd_alerts(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Usage: /alerts — показать активные ценовые алерты"""
        self._register_chat(update)
        alerts = self.alert_manager.list_alerts()
        if not alerts:
            await update.message.reply_text("📭 Нет активных алертов\n"
                                             "Торговый: `/alert SYMBOL PRICE SIDE USDT SL LEV`\n"
                                             "Уведомление: `/notify SYMBOL PRICE`",
                                             parse_mode=ParseMode.MARKDOWN)
            return

        header = f"*🔔 Активные алерты ({len(alerts)}):*\n"
        chunks = []
        current = [header]
        max_len = 3500  # Telegram limit is 4096; keep margin for Markdown.

        for a in alerts:
            direction = "📉 падение до" if a.direction == "down" else "📈 рост до"

            if a.alert_type == "notify":
                item = (
                    f"*#{a.id}* `{a.symbol}` 🔔 УВЕДОМЛЕНИЕ\n"
                    f"  {direction} `{a.trigger_price}`\n"
                )
            else:
                stop_size = abs(a.trigger_price - a.sl_price) if a.sl_price > 0 else 0
                sl_str = f" | SL: `{a.sl_price}`" if a.sl_price > 0 else ""
                stop_str = f" | Stop: `{stop_size}`" if stop_size > 0 else ""
                item = (
                    f"*#{a.id}* `{a.symbol}` {a.side.upper()}\n"
                    f"  {direction} `{a.trigger_price}`{sl_str}{stop_str}\n"
                    f"  Объём: `{a.usdt_amount} USDT` | Плечо: `{a.leverage}x`\n"
                )

            candidate = "\n".join(current + [item])
            if len(candidate) > max_len and len(current) > 1:
                chunks.append("\n".join(current))
                current = ["*🔔 Активные алерты, продолжение:*\n", item]
            else:
                current.append(item)

        if current:
            chunks.append("\n".join(current))

        for chunk in chunks:
            await update.message.reply_text(
                chunk, parse_mode=ParseMode.MARKDOWN
            )

    # ── ATR (Average True Range) ─────────────────────────────────────────────
    @restricted
    async def cmd_atr(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """
        /atr SYMBOL — ATR(21) + дистанция пройденная за сегодня
        """
        self._register_chat(update)
        if not ctx.args:
            await update.message.reply_text(
                "❌ Использование: `/atr SYMBOL`\n"
                "Пример: `/atr XMR` или `/atr SOLUSDTM`",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        symbol = ctx.args[0].upper()
        if symbol.endswith("USDTM"):
            pass
        elif symbol.endswith("USDT"):
            symbol += "M"
        else:
            symbol += "USDTM"

        try:
            # Получаем 25 дневных свечей (22 нужно для ATR21 + запас)
            klines = await self.client.get_klines(symbol, granularity=1440, count=25)

            if not klines or len(klines) < 2:
                await update.message.reply_text(
                    f"❌ Недостаточно данных для `{symbol}`",
                    parse_mode=ParseMode.MARKDOWN
                )
                return

            # Сортируем по времени (старые -> новые)
            klines.sort(key=lambda x: x[0])

            # True Range для каждой свечи
            # TR = max(high - low, |high - prev_close|, |low - prev_close|)
            tr_values = []
            for i in range(1, len(klines)):
                high       = float(klines[i][2])
                low        = float(klines[i][3])
                prev_close = float(klines[i - 1][4])

                tr = max(
                    high - low,
                    abs(high - prev_close),
                    abs(low - prev_close)
                )
                tr_values.append(tr)

            # ATR(21)
            period = min(21, len(tr_values))
            atr = sum(tr_values[-period:]) / period

            # Сегодняшняя свеча (последняя)
            today = klines[-1]
            today_high  = float(today[2])
            today_low   = float(today[3])
            today_open  = float(today[1])
            today_close = float(today[4])
            today_range = today_high - today_low

            # Текущая цена
            current_price = today_close
            atr_pct = (atr / current_price * 100) if current_price > 0 else 0

            # Сколько от ATR пройдено сегодня
            if atr > 0:
                atr_used = today_range / atr * 100
            else:
                atr_used = 0

            # Направление дня
            if today_close >= today_open:
                day_dir = "📈"
                day_move = today_close - today_open
            else:
                day_dir = "📉"
                day_move = today_open - today_close
            day_move_pct = (day_move / today_open * 100) if today_open > 0 else 0

            await update.message.reply_text(
                f"📊 *ATR({period}) — `{symbol}`*\n\n"
                f"ATR: `{atr:.6f}` (`{atr_pct:.2f}%` от цены)\n\n"
                f"*Сегодня:*\n"
                f"  Диапазон: `{today_range:.6f}` (H `{today_high}` / L `{today_low}`)\n"
                f"  Пройдено ATR: `{atr_used:.1f}%`\n"
                f"  Движение: {day_dir} `{day_move:.6f}` (`{day_move_pct:.2f}%`)\n"
                f"  Цена: `{current_price}`",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

    # ── Kill (emergency stop) ────────────────────────────────────────────────
    @restricted
    async def cmd_kill(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Usage: /kill — форсированная остановка бота"""
        self._register_chat(update)
        await update.message.reply_text(
            "🛑 *Бот останавливается…*\n"
            "Все алерты сохранены. Активные ордера на бирже остаются.",
            parse_mode=ParseMode.MARKDOWN
        )
        logger.info("Kill command received from Telegram — shutting down")

        # Останавливаем монитор → это завершит run() → main() выйдет
        await self.monitor.stop()

        # Принудительный выход через 3 секунды если не завершился
        asyncio.get_event_loop().call_later(3, lambda: os._exit(0))

    # ── Unknown command ───────────────────────────────────────────────────────
    async def cmd_unknown(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "❓ Неизвестная команда. Введите /help для списка команд."
        )

    # ── Build & run ───────────────────────────────────────────────────────────
    def build(self) -> Application:
        app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
        self._app = app

        app.add_handler(CommandHandler("start",     self.cmd_start))
        app.add_handler(CommandHandler("help",      self.cmd_help))
        app.add_handler(CommandHandler("status",    self.cmd_status))
        app.add_handler(CommandHandler("positions", self.cmd_positions))
        app.add_handler(CommandHandler("orders",    self.cmd_orders))
        app.add_handler(CommandHandler("open",      self.cmd_open))
        app.add_handler(CommandHandler("stop",      self.cmd_stop_entry))
        app.add_handler(CommandHandler("market",    self.cmd_market))
        app.add_handler(CommandHandler("trailing",  self.cmd_trailing))
        app.add_handler(CommandHandler("close",     self.cmd_close))
        app.add_handler(CommandHandler("cancel",    self.cmd_cancel))
        app.add_handler(CommandHandler("cancelall", self.cmd_cancelall))
        app.add_handler(CommandHandler("leverage",  self.cmd_leverage))
        app.add_handler(CommandHandler("price",     self.cmd_price))
        app.add_handler(CommandHandler("alert",     self.cmd_alert))
        app.add_handler(CommandHandler("notify",    self.cmd_notify))
        app.add_handler(CommandHandler("alerts",    self.cmd_alerts))
        app.add_handler(CommandHandler("rmalert",   self.cmd_rmalert))
        app.add_handler(CommandHandler("clearalerts", self.cmd_clearalerts))
        app.add_handler(CommandHandler("kill",       self.cmd_kill))
        app.add_handler(CommandHandler("atr",        self.cmd_atr))
        app.add_handler(MessageHandler(filters.COMMAND, self.cmd_unknown))

        return app

    async def run(self):
        """Start WebSocket monitor + Telegram polling concurrently."""
        config.validate()

        # Wire up monitor events → manager handlers
        self.monitor.on("order_filled",          self.manager.on_order_filled)
        self.monitor.on("trailing_stop_triggered", self.manager.on_trailing_stop_triggered)
        self.monitor.on("position_opened",       self.manager.on_position_opened)
        self.monitor.on("price_update",          self.manager.on_price_update)

        # Wire up monitor events → alert manager (ценовые алерты)
        self.monitor.on("price_update",          self.alert_manager.on_price_update)

        app = self.build()

        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)
        logger.info("Telegram bot started")

        await self.manager.reconcile_restored_plans()
        for symbol in self.manager.get_plan_symbols():
            self.monitor.subscribe_ticker(symbol)

        try:
            await self.monitor.start()
        except (asyncio.CancelledError, KeyboardInterrupt):
            logger.info("Shutting down…")
        finally:
            # Корректное завершение в правильном порядке
            try:
                await self.monitor.stop()
            except Exception:
                pass
            try:
                await app.updater.stop()
            except Exception:
                pass
            try:
                await app.stop()
            except Exception:
                pass
            try:
                await app.shutdown()
            except Exception:
                pass
            try:
                await self.client.close()
            except Exception:
                pass
            logger.info("Bot shutdown complete")
