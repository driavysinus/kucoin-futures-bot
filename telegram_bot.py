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
"""

import asyncio
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


class TradingBot:
    def __init__(self):
        self.client  = KuCoinFuturesClient()
        self.monitor = FuturesMonitor(self.client)
        self.manager = OrderManager(self.client, notify=self._broadcast)
        self._chat_ids: set[int] = set()
        self._app: Application = None

    # ── Broadcast ─────────────────────────────────────────────────────────────
    async def _broadcast(self, text: str):
        if not self._app:
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
        self._chat_ids.add(update.effective_chat.id)
        await update.message.reply_text(
            "🤖 *KuCoin Futures Bot активирован*\n\n"
            "Введите /help для списка команд.",
            parse_mode=ParseMode.MARKDOWN
        )

    @restricted
    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "*📖 Команды бота:*\n\n"
            "*Информация:*\n"
            "`/status` — баланс аккаунта\n"
            "`/positions` — открытые позиции\n"
            "`/orders [SYMBOL]` — активные ордера\n"
            "`/price SYMBOL` — текущая цена\n\n"
            "*Торговля (размер всегда в USDT):*\n"
            "`/open SYMBOL SIDE USDT PRICE [CB%] [TRIG%] [CLOSE%] [LEV]`\n"
            "  → лимитный ордер на вход\n"
            "  Пример: `/open SKYUSDTM buy 9 0.0783 10 10 50 3`\n\n"
            "`/stop SYMBOL SIDE USDT PRICE [CB%] [TRIG%] [CLOSE%] [LEV]`\n"
            "  → стоп-маркет ордер на вход *заблаговременно*\n"
            "  buy: активируется когда цена *вырастет* до PRICE\n"
            "  sell: активируется когда цена *упадёт* до PRICE\n"
            "  Пример: `/stop SKYUSDTM buy 9 0.0800 10 10 50 3`\n\n"
            "  Оба ордера после исполнения автоматически:\n"
            "  🔁 Трейлинг-стоп → ✂️ Порез → 🎯 Безубыток\n\n"
            "`/trailing SYMBOL SIDE USDT CB% ACTIVATE [TRIG%] [CLOSE%] [LEV]`\n"
            "  → трейлинг-стоп на уже открытую позицию\n\n"
            "`/close SYMBOL [PCT%]` — ручной порез позиции\n"
            "`/market SYMBOL SIDE USDT [LEV]` — рыночный ордер\n\n"
            "*Управление:*\n"
            "`/cancel ORDER_ID` — отмена ордера\n"
            "`/cancelall SYMBOL` — отмена всех ордеров\n"
            "`/leverage SYMBOL VALUE` — установить плечо\n",
            parse_mode=ParseMode.MARKDOWN
        )

    @restricted
    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        self._chat_ids.add(update.effective_chat.id)
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
        self._chat_ids.add(update.effective_chat.id)
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
        self._chat_ids.add(update.effective_chat.id)
        symbol = _parse(ctx.args, 0)
        try:
            orders = await self.client.get_open_orders(symbol)
            if not orders:
                await update.message.reply_text("📭 Нет активных ордеров")
                return
            lines = [f"*📋 Активные ордера{' (' + symbol + ')' if symbol else ''}:*\n"]
            for o in orders[:15]:   # cap at 15 to avoid message length limit
                otype = o.get("type", "")
                label = "🔁 Трейлинг" if "trailing" in otype else "📋 Лимит"
                lines.append(
                    f"{label} `{o.get('id', '')[:12]}…`\n"
                    f"  {o.get('symbol')} | {'BUY' if o.get('side')=='buy' else 'SELL'} "
                    f"| Размер: `{o.get('size')}` | Цена: `{o.get('price', 'Market')}`\n"
                )
            await update.message.reply_text(
                "\n".join(lines), parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

    @restricted
    async def cmd_open(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """
        /open SYMBOL SIDE USDT PRICE SL TRIM% LEV
        """
        self._chat_ids.add(update.effective_chat.id)
        args = ctx.args
        if len(args) < 6:
            await update.message.reply_text(
                "❌ Использование:\n"
                "`/open SYMBOL SIDE USDT PRICE SL TRIM% LEV`\n\n"
                "Пример:\n"
                "`/open TRUMPUSDTM buy 9 4.020 3.653 10 3`\n"
                "  USDT=9 | Цена=4.020 | SL=3.653 | Порез при +10% | Плечо 3x\n\n"
                "`/open SKYUSDTM sell 9 0.085 0.092 10 5`\n"
                "  Шорт | Цена=0.085 | SL=0.092 | Порез при +10% | Плечо 5x",
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
            sl_price    = _parse(args, 4, float, 0.0)   # 0 = без стопа
            trim_pct    = _parse(args, 5, float, config.DEFAULT_PROFIT_TRIGGER_PCT)
            lev         = _parse(args, 6, int,   config.DEFAULT_LEVERAGE)

            self.monitor.subscribe_ticker(symbol)
            self.manager.set_leverage(symbol, lev)

            await self.manager.place_limit_order(
                symbol, side, usdt_amount, price,
                sl_price=sl_price, trim_pct=trim_pct, leverage=lev,
            )
        except Exception as e:
            logger.error(f"cmd_open error: {e}", exc_info=True)
            await update.message.reply_text(f"❌ Ошибка: {e}")

    @restricted
    async def cmd_stop_entry(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """
        /stop SYMBOL SIDE USDT PRICE [CB%] [TRIG%] [CLOSE%] [LEV]
        Стоп-маркет ордер на вход — выставляется заблаговременно.
        buy:  активируется когда цена вырастет до PRICE
        sell: активируется когда цена упадёт до PRICE
        """
        self._chat_ids.add(update.effective_chat.id)
        args = ctx.args
        if len(args) < 4:
            await update.message.reply_text(
                "❌ Использование:\n"
                "`/stop SYMBOL SIDE USDT PRICE [CB%] [TRIG%] [CLOSE%] [LEV]`\n\n"
                "Пример:\n"
                "`/stop SKYUSDTM buy 9 0.0800 10 10 50 3`\n"
                "  → Когда цена вырастет до `0.0800` — купить лонг на 9 USDT\n"
                "  → Трейлинг-стоп callback `10%`\n"
                "  → При `+10%` профита → порез `50%` + стоп в безубыток\n\n"
                "`/stop SKYUSDTM sell 9 0.0750 10 10 50 3`\n"
                "  → Когда цена упадёт до `0.0750` — открыть шорт на 9 USDT",
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
            usdt_amount   = _float(args[2])
            price         = _float(args[3])
            sl_price      = _parse(args, 4, float, 0.0)
            callback_rate = _parse(args, 5, float, config.DEFAULT_TRAILING_STOP_PCT)
            trigger       = _parse(args, 6, float, config.DEFAULT_PROFIT_TRIGGER_PCT)
            close_pct     = _parse(args, 7, float, config.DEFAULT_PARTIAL_CLOSE_PCT)
            lev           = _parse(args, 8, int,   config.DEFAULT_LEVERAGE)

            self.manager.set_leverage(symbol, lev)
            self.monitor.subscribe_ticker(symbol)

            await self.manager.place_stop_entry(
                symbol, side, usdt_amount, price,
                leverage=lev, callback_rate=callback_rate,
                profit_trigger=trigger, partial_close_pct=close_pct,
                sl_price=sl_price,
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")


    @restricted
    async def cmd_market(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Usage: /market SYMBOL SIDE USDT [LEVERAGE]"""
        self._chat_ids.add(update.effective_chat.id)
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
        self._chat_ids.add(update.effective_chat.id)
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
        self._chat_ids.add(update.effective_chat.id)
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
        self._chat_ids.add(update.effective_chat.id)
        if not ctx.args:
            await update.message.reply_text("❌ Использование: `/cancel ORDER_ID`",
                                             parse_mode=ParseMode.MARKDOWN)
            return
        await self.manager.cancel_order(ctx.args[0])

    @restricted
    async def cmd_cancelall(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Usage: /cancelall SYMBOL"""
        self._chat_ids.add(update.effective_chat.id)
        if not ctx.args:
            await update.message.reply_text("❌ Использование: `/cancelall SYMBOL`",
                                             parse_mode=ParseMode.MARKDOWN)
            return
        await self.manager.cancel_all(ctx.args[0].upper())

    @restricted
    async def cmd_leverage(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Usage: /leverage SYMBOL VALUE"""
        self._chat_ids.add(update.effective_chat.id)
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
        self._chat_ids.add(update.effective_chat.id)
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

        app = self.build()

        async with app:
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            logger.info("Telegram bot started")

            # Run WS monitor alongside
            try:
                await self.monitor.start()
            finally:
                await app.updater.stop()
                await app.stop()
                await app.shutdown()
                await self.client.close()