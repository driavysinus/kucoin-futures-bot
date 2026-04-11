"""
console_input.py

Асинхронный ввод команд из консоли.

Алерты:
  add SYMBOL PRICE SIDE USDT [SL] [TRIG%] [LEV]   — ценовой алерт
  list                                              — активные алерты
  remove ID                                         — удалить алерт
  clear [SYMBOL]                                    — удалить все алерты

Ордера и позиции:
  orders [SYMBOL]                                   — активные ордера на бирже
  positions                                         — открытые позиции
  cancel ORDER_ID                                   — отменить ордер
  cancelall SYMBOL                                  — отменить все ордера по символу
  close SYMBOL [PCT]                                — порез позиции (по умолч. 50%)
  price SYMBOL                                      — текущая цена

  help                                              — справка
"""

import asyncio
import sys
from loguru import logger


class ConsoleInput:
    def __init__(self, alert_manager, order_manager=None, client=None, monitor=None):
        """
        alert_manager: AlertManager
        order_manager: OrderManager   — для cancel/close/orders
        client:        KuCoinFuturesClient — для REST запросов (orders, positions, price)
        monitor:       FuturesMonitor — для кэша цен
        """
        self.alert_manager = alert_manager
        self.order_manager = order_manager
        self.client        = client
        self.monitor       = monitor

    async def start(self):
        """Запустить чтение stdin в asyncio."""
        logger.info("Console input ready. Type 'help' for commands.")
        loop = asyncio.get_event_loop()

        while True:
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    logger.info("stdin closed, console input disabled")
                    return

                line = line.strip()
                if not line:
                    continue

                await self._process_command(line)

            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error(f"Console input error: {e}")

    async def _process_command(self, line: str):
        """Парсим и выполняем консольную команду."""
        parts = line.split()
        cmd   = parts[0].lower()

        if cmd == "help":
            self._print_help()

        # ── Алерты ────────────────────────────────────────────────────────
        elif cmd == "notify":
            await self._cmd_notify(parts[1:])
        elif cmd == "add":
            await self._cmd_add(parts[1:])
        elif cmd == "list":
            self._cmd_list()
        elif cmd == "remove" or cmd == "rm":
            self._cmd_remove(parts[1:])
        elif cmd == "clear":
            self._cmd_clear(parts[1:])

        # ── Ордера и позиции ──────────────────────────────────────────────
        elif cmd == "orders":
            await self._cmd_orders(parts[1:])
        elif cmd == "positions" or cmd == "pos":
            await self._cmd_positions()
        elif cmd == "cancel":
            await self._cmd_cancel(parts[1:])
        elif cmd == "cancelall":
            await self._cmd_cancelall(parts[1:])
        elif cmd == "close":
            await self._cmd_close(parts[1:])
        elif cmd == "price":
            await self._cmd_price(parts[1:])
        elif cmd == "kill":
            await self._cmd_kill()

        else:
            print(f"  ❓ Неизвестная команда: {cmd}. Введите 'help'.")

    # ══════════════════════════════════════════════════════════════════════
    #  АЛЕРТЫ
    # ══════════════════════════════════════════════════════════════════════

    async def _cmd_notify(self, args: list):
        """notify SYMBOL PRICE — уведомление при достижении цены."""
        if len(args) < 2:
            print("  ❌ Использование: notify SYMBOL PRICE")
            print("  Пример: notify XMR 330")
            return

        try:
            symbol = args[0].upper()
            if not symbol.endswith("USDTM"):
                symbol = symbol.rstrip("USDT") + "USDTM" if symbol.endswith("USDT") else symbol + "USDTM"

            price = float(args[1].replace(",", "."))

            alert = await self.alert_manager.add_notify_alert(symbol, price)
            direction_emoji = "📉" if alert.direction == "down" else "📈"
            print(
                f"  ✅ Уведомление #{alert.id} добавлено\n"
                f"     {symbol} {direction_emoji} @ {price}\n"
                f"     Только уведомление (без сделки)"
            )
        except (ValueError, IndexError) as e:
            print(f"  ❌ Ошибка: {e}")

    async def _cmd_add(self, args: list):
        """add SYMBOL PRICE SIDE USDT [SL] [TRIG%] [LEV]"""
        if len(args) < 4:
            print("  ❌ Использование: add SYMBOL PRICE SIDE USDT [SL] [TRIG%] [LEV]")
            print("  Пример: add XBTUSDTM 70000 buy 100 68000 2 10")
            return

        try:
            symbol = args[0].upper()
            # Нормализация символа:
            #   dogeusdt  → DOGEUSDTM
            #   doge      → DOGEUSDTM
            #   DOGEUSDTM → DOGEUSDTM (без изменений)
            if symbol.endswith("USDTM"):
                pass  # уже правильный формат
            elif symbol.endswith("USDT"):
                symbol = symbol + "M"  # DOGEUSDT → DOGEUSDTM
            else:
                symbol = symbol + "USDTM"  # DOGE → DOGEUSDTM

            price       = float(args[1].replace(",", "."))
            side        = args[2].lower()
            usdt_amount = float(args[3].replace(",", "."))

            if side not in ("buy", "sell"):
                print("  ❌ SIDE должен быть 'buy' или 'sell'")
                return

            sl_price = float(args[4].replace(",", ".")) if len(args) > 4 else 0.0
            trim_pct = float(args[5].replace(",", ".")) if len(args) > 5 else None
            leverage = int(args[6])                      if len(args) > 6 else None

            alert = await self.alert_manager.add_alert(
                symbol=symbol,
                trigger_price=price,
                side=side,
                usdt_amount=usdt_amount,
                sl_price=sl_price,
                trim_pct=trim_pct,
                leverage=leverage,
            )

            direction_emoji = "📉 ждём падения до" if alert.direction == "down" else "📈 ждём роста до"
            sl_str = f" | SL={sl_price}" if sl_price > 0 else ""
            print(
                f"  ✅ Алерт #{alert.id} добавлен\n"
                f"     {symbol} {side.upper()} {usdt_amount} USDT\n"
                f"     Триггер: {direction_emoji} {price}{sl_str}\n"
                f"     Тейки каждые +{alert.trim_pct}% | Плечо {alert.leverage}x"
            )

        except (ValueError, IndexError) as e:
            print(f"  ❌ Ошибка парсинга: {e}")
            print("  Формат: add SYMBOL PRICE SIDE USDT [SL] [TRIG%] [LEV]")

    def _cmd_list(self):
        alerts = self.alert_manager.list_alerts()
        if not alerts:
            print("  📭 Нет активных алертов")
            return

        print(f"  📋 Активные алерты ({len(alerts)}):")
        print(f"  {'ID':>4}  {'Символ':<14} {'Сторона':<6} {'Триггер':>12} "
              f"{'USDT':>8} {'SL':>10} {'Trig%':>6} {'Lev':>4}")
        print(f"  {'─'*4}  {'─'*14} {'─'*6} {'─'*12} {'─'*8} {'─'*10} {'─'*6} {'─'*4}")

        for a in alerts:
            if a.alert_type == "notify":
                dir_emoji = "📉" if a.direction == "down" else "📈"
                print(f"  {a.id:>4}  {a.symbol:<14} {'🔔':>6} {a.trigger_price:>12} "
                      f"{'—':>8} {'—':>10} {'—':>6} {'—':>5}  {dir_emoji} уведомление")
            else:
                sl_str = str(a.sl_price) if a.sl_price > 0 else "—"
                print(f"  {a.id:>4}  {a.symbol:<14} {a.side:<6} {a.trigger_price:>12} "
                      f"{a.usdt_amount:>8} {sl_str:>10} {a.trim_pct:>6} {a.leverage:>4}x")

    def _cmd_remove(self, args: list):
        if not args:
            print("  ❌ Использование: remove ID")
            return
        try:
            alert_id = int(args[0])
            removed  = self.alert_manager.remove_alert(alert_id)
            if removed:
                print(f"  🗑  Алерт #{alert_id} удалён ({removed.symbol} {removed.side} @ {removed.trigger_price})")
            else:
                print(f"  ❌ Алерт #{alert_id} не найден")
        except ValueError:
            print("  ❌ ID должен быть числом")

    def _cmd_clear(self, args: list):
        symbol = args[0].upper() if args else None
        before = len(self.alert_manager.list_alerts())
        self.alert_manager.clear_alerts(symbol)
        after  = len(self.alert_manager.list_alerts())
        removed = before - after
        suffix = f" по {symbol}" if symbol else ""
        print(f"  🗑  Удалено {removed} алертов{suffix}")

    # ══════════════════════════════════════════════════════════════════════
    #  ОРДЕРА И ПОЗИЦИИ
    # ══════════════════════════════════════════════════════════════════════

    async def _cmd_orders(self, args: list):
        """orders [SYMBOL] — показать активные ордера на бирже"""
        if not self.client:
            print("  ❌ Клиент не подключён")
            return

        symbol = args[0].upper() if args else None
        try:
            orders = await self.client.get_open_orders(symbol)
            if not orders:
                suffix = f" по {symbol}" if symbol else ""
                print(f"  📭 Нет активных ордеров{suffix}")
                return

            suffix = f" ({symbol})" if symbol else ""
            print(f"  📋 Активные ордера{suffix}: {len(orders)} шт.\n")
            print(f"  {'Тип':<16} {'Символ':<14} {'Сторона':<6} {'Размер':>8} {'Цена':>14} {'ID'}")
            print(f"  {'─'*16} {'─'*14} {'─'*6} {'─'*8} {'─'*14} {'─'*36}")

            for o in orders[:20]:
                is_stop  = bool(o.get("stop"))
                is_trail = "trailing" in str(o.get("trailingStop", ""))
                reduce   = o.get("reduceOnly", False)

                if is_trail:
                    label = "🔁 Трейлинг"
                elif is_stop and reduce:
                    label = "🎯 Стоп-закр."
                elif is_stop:
                    label = "🎯 Стоп-вход"
                else:
                    label = "📋 Лимит"

                stop_price = o.get("stopPrice", "")
                price      = o.get("price", "0")
                show_price = stop_price if is_stop else price
                side       = "BUY" if o.get("side") == "buy" else "SELL"
                sym        = o.get("symbol", "")
                size       = o.get("size", "")
                oid        = o.get("id", o.get("orderId", ""))

                print(f"  {label:<16} {sym:<14} {side:<6} {size:>8} {show_price:>14} {oid}")

        except Exception as e:
            print(f"  ❌ Ошибка: {e}")

    async def _cmd_positions(self):
        """positions — показать открытые позиции"""
        if not self.client:
            print("  ❌ Клиент не подключён")
            return

        try:
            positions = await self.client.get_positions()
            active = [p for p in positions if float(p.get("currentQty", 0)) != 0]
            if not active:
                print("  📭 Нет открытых позиций")
                return

            print(f"  📊 Открытые позиции: {len(active)} шт.")
            for p in active:
                qty   = float(p.get("currentQty", 0))
                side  = "LONG 📈" if qty > 0 else "SHORT 📉"
                entry = p.get("avgEntryPrice", "N/A")
                liq   = p.get("liquidationPrice", "N/A")
                pnl   = float(p.get("unrealisedPnl", 0))
                pnl_e = "🟢" if pnl >= 0 else "🔴"
                sym   = p.get("symbol", "")

                print(
                    f"\n  {sym} | {side}\n"
                    f"    Объём: {abs(qty)} контрактов\n"
                    f"    Вход: {entry} | Ликв.: {liq}\n"
                    f"    PnL: {pnl_e} {pnl:.4f} USDT"
                )

        except Exception as e:
            print(f"  ❌ Ошибка: {e}")

    async def _cmd_cancel(self, args: list):
        """cancel ORDER_ID — отменить ордер"""
        if not self.order_manager:
            print("  ❌ OrderManager не подключён")
            return
        if not args:
            print("  ❌ Использование: cancel ORDER_ID")
            return

        order_id = args[0]
        try:
            ok = await self.order_manager.cancel_order(order_id)
            if ok:
                print(f"  🗑  Ордер {order_id} отменён")
        except Exception as e:
            print(f"  ❌ Ошибка: {e}")

    async def _cmd_cancelall(self, args: list):
        """cancelall SYMBOL — отменить все ордера по символу"""
        if not self.order_manager:
            print("  ❌ OrderManager не подключён")
            return
        if not args:
            print("  ❌ Использование: cancelall SYMBOL")
            return

        symbol = args[0].upper()
        try:
            ok = await self.order_manager.cancel_all(symbol)
            if ok:
                print(f"  🗑  Все ордера по {symbol} отменены")
        except Exception as e:
            print(f"  ❌ Ошибка: {e}")

    async def _cmd_close(self, args: list):
        """close SYMBOL [PCT] — порез позиции"""
        if not self.order_manager:
            print("  ❌ OrderManager не подключён")
            return
        if not args:
            print("  ❌ Использование: close SYMBOL [PCT]")
            print("  Пример: close XBTUSDTM 50")
            return

        symbol = args[0].upper()
        try:
            pct = float(args[1].replace(",", ".")) if len(args) > 1 else 50.0
        except ValueError:
            pct = 50.0

        try:
            oid = await self.order_manager.partial_close(symbol, pct, "Ручной порез (консоль)")
            if oid:
                print(f"  ✂️  Порез {pct}% по {symbol} — ордер {oid}")
            else:
                print(f"  ⚠️  Нет позиции по {symbol} или уже закрыта")
        except Exception as e:
            print(f"  ❌ Ошибка: {e}")

    async def _cmd_price(self, args: list):
        """price SYMBOL — текущая цена"""
        if not args:
            print("  ❌ Использование: price SYMBOL")
            return

        symbol = args[0].upper()
        if symbol.endswith("USDTM"):
            pass
        elif symbol.endswith("USDT"):
            symbol += "M"
        else:
            symbol += "USDTM"

        try:
            price = None
            if self.monitor:
                price = self.monitor.get_price(symbol)
            if not price and self.client:
                price = await self.client.get_mark_price(symbol)

            if price:
                print(f"  💹 {symbol}: {price}")
            else:
                print(f"  ❌ Цена для {symbol} недоступна")
        except Exception as e:
            print(f"  ❌ Ошибка: {e}")

    async def _cmd_kill(self):
        """kill — форсированная остановка бота"""
        print("  🛑 Бот останавливается…")
        logger.info("Kill command received from console — shutting down")
        import os
        os._exit(0)

    # ══════════════════════════════════════════════════════════════════════
    #  HELP
    # ══════════════════════════════════════════════════════════════════════

    def _print_help(self):
        print("""
  ╔══════════════════════════════════════════════════════════════════╗
  ║                     КОНСОЛЬНЫЕ КОМАНДЫ                         ║
  ╠══════════════════════════════════════════════════════════════════╣
  ║                                                                ║
  ║  ── АЛЕРТЫ ──────────────────────────────────────────────────── ║
  ║                                                                ║
  ║  add SYMBOL PRICE SIDE USDT [SL] [TRIG%] [LEV]                ║
  ║      Добавить ценовой алерт с автооткрытием позиции.           ║
  ║                                                                ║
  ║      SYMBOL — торговый символ (XBTUSDTM, BTC и т.д.)          ║
  ║      PRICE  — цена срабатывания                                ║
  ║      SIDE   — buy (long) или sell (short)                      ║
  ║      USDT   — размер позиции в USDT                           ║
  ║      SL     — стоп-лосс (0 = без стопа)                       ║
  ║      TRIG%  — % для тейков (по умолч. из .env)                 ║
  ║      LEV    — плечо (по умолч. из .env)                        ║
  ║                                                                ║
  ║  Примеры:                                                      ║
  ║    add XBTUSDTM 70000 buy 100 68000 2 10                       ║
  ║    add ETHUSDTM 4000 sell 50 4200 1.5 5                        ║
  ║    add BTC 95000 buy 20 93000 1 3                              ║
  ║                                                                ║
  ║  notify SYMBOL PRICE — уведомление при цене (без сделки)        ║
  ║  list               — показать активные алерты                 ║
  ║  remove ID          — удалить алерт по номеру                  ║
  ║  clear [SYMBOL]     — удалить все алерты (или по символу)      ║
  ║                                                                ║
  ║  ── ОРДЕРА И ПОЗИЦИИ ────────────────────────────────────────── ║
  ║                                                                ║
  ║  orders [SYMBOL]    — активные ордера на бирже                 ║
  ║  positions          — открытые позиции                         ║
  ║  cancel ORDER_ID    — отменить ордер                           ║
  ║  cancelall SYMBOL   — отменить все ордера по символу           ║
  ║  close SYMBOL [PCT] — порез позиции (по умолч. 50%)            ║
  ║  price SYMBOL       — текущая цена                             ║
  ║                                                                ║
  ║  ── ЭКСТРЕННОЕ ──────────────────────────────────────────────── ║
  ║                                                                ║
  ║  kill                — 🛑 форсированная остановка бота          ║
  ║                                                                ║
  ║  help               — эта справка                              ║
  ║                                                                ║
  ╚══════════════════════════════════════════════════════════════════╝
""")
