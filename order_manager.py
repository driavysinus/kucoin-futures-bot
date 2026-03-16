"""
order_manager.py

/open SYMBOL SIDE USDT PRICE SL TRIG% LEV

Флоу:
  1. Лимитный ордер на вход (REST)
  2. После исполнения (WebSocket on_order_filled):
     - Стоп-маркет тейк 50% на цене входа +/- TRIG% (reduceOnly)
     - Стоп-маркет стоп-лосс на SL (reduceOnly)
  3. При исполнении тейка (WebSocket on_order_filled):
     - Отмена старого стоп-лосса (REST)
     - Новый стоп-маркет в безубыток для оставшихся 50% (REST, reduceOnly)
"""

import asyncio
from dataclasses import dataclass
from typing import Optional, Callable
from loguru import logger

from kucoin_client import KuCoinFuturesClient
import config


@dataclass
class Plan:
    symbol:         str
    side:           str       # "buy" / "sell"
    entry_price:    float     # цена фактического исполнения
    contracts:      int       # полный размер позиции в контрактах
    sl_price:       float     # стоп-лосс цена
    trim_pct:       float     # % движения для тейка
    leverage:       int
    entry_order_id: Optional[str] = None
    sl_order_id:    Optional[str] = None   # стоп-маркет стоп-лосс
    take_order_id:  Optional[str] = None   # стоп-маркет тейк 50%
    filled:  bool = False   # лимитка исполнена
    taken:   bool = False   # тейк исполнен


class OrderManager:
    def __init__(self, client: KuCoinFuturesClient, notify: Callable = None):
        self.client   = client
        self._notify  = notify or (lambda msg: None)
        self._plans:    dict[str, Plan] = {}
        self._leverage: dict[str, int] = {}

    def set_leverage(self, symbol: str, lev: int):
        self._leverage[symbol] = lev

    def get_leverage(self, symbol: str) -> int:
        return self._leverage.get(symbol, config.DEFAULT_LEVERAGE)

    async def _send(self, msg: str):
        try:
            if asyncio.iscoroutinefunction(self._notify):
                await self._notify(msg)
            else:
                self._notify(msg)
        except Exception as e:
            logger.error(f"Notify error: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # ШАГ 1: /open — лимитный ордер на вход
    # ─────────────────────────────────────────────────────────────────────────
    async def place_limit_order(self, symbol: str, side: str,
                                usdt_amount: float, price: float,
                                sl_price: float, trim_pct: float,
                                leverage: int) -> str:

        # Если есть старый план — отменяем его ордера
        old = self._plans.get(symbol)
        if old:
            for oid in [old.sl_order_id, old.take_order_id]:
                if oid:
                    try:
                        await self.client.cancel_order(oid)
                        logger.info(f"Cancelled old order {oid} for {symbol}")
                    except Exception as e:
                        logger.warning(f"Could not cancel old order {oid}: {e}")

        contracts, _, multiplier = await self.client.usdt_to_contracts(
            symbol, usdt_amount, price
        )
        actual_usdt = contracts * price * multiplier

        data     = await self.client.place_limit_order(symbol, side, contracts, price, leverage)
        order_id = data.get("orderId", "")

        self._plans[symbol] = Plan(
            symbol=symbol, side=side,
            entry_price=price, contracts=contracts,
            sl_price=sl_price, trim_pct=trim_pct,
            leverage=leverage, entry_order_id=order_id,
        )

        sl_line = f"\n  🛑 Стоп-лосс: `{sl_price}`" if sl_price > 0 else ""
        logger.info(f"Limit order: {symbol} {side} {contracts}c @{price} SL={sl_price} → {order_id}")
        await self._send(
            f"📋 *Лимитный ордер выставлен*\n"
            f"Символ: `{symbol}`\n"
            f"Направление: `{'LONG 📈' if side=='buy' else 'SHORT 📉'}`\n"
            f"Размер: `{actual_usdt:.2f} USDT` → `{contracts}` контрактов\n"
            f"Цена: `{price}`  |  Плечо: `{leverage}x`\n"
            f"После исполнения:{sl_line}\n"
            f"  ✂️ Тейк 50% при движении `+{trim_pct}%`\n"
            f"  🎯 Безубыток после тейка\n"
            f"ID: `{order_id}`"
        )
        return order_id

    # ─────────────────────────────────────────────────────────────────────────
    # ШАГ 2: после исполнения лимитки — выставляем тейк и SL
    # ─────────────────────────────────────────────────────────────────────────
    async def _on_entry_filled(self, plan: Plan, fill_price: float):
        plan.entry_price = fill_price
        close_side  = "sell" if plan.side == "buy" else "buy"
        half        = max(1, plan.contracts // 2)

        logger.info(f"_on_entry_filled: symbol={plan.symbol} side={plan.side} "
                    f"fill={fill_price} sl={plan.sl_price} trim={plan.trim_pct}%")

        # Цена тейка: лонг → выше на trim_pct%, шорт → ниже
        if plan.side == "buy":
            take_price     = round(fill_price * (1 + plan.trim_pct / 100), 8)
            take_direction = "up"
            sl_direction   = "down"
        else:
            take_price     = round(fill_price * (1 - plan.trim_pct / 100), 8)
            take_direction = "down"
            sl_direction   = "up"

        # Тейк-профит 50% — стоп-маркет reduceOnly
        try:
            data = await self.client.place_stop_market_close(
                plan.symbol, close_side, half,
                take_price, take_direction, plan.leverage
            )
            plan.take_order_id = data.get("orderId", "")
            logger.info(f"Take order: {plan.symbol} {half}c @ {take_price} → {plan.take_order_id}")
            await self._send(
                f"✂️ *Тейк-профит 50% выставлен*\n"
                f"Символ: `{plan.symbol}`\n"
                f"Цена входа: `{fill_price}`\n"
                f"Цена тейка: `{take_price}` (`+{plan.trim_pct}%`)\n"
                f"Контрактов: `{half}` из `{plan.contracts}`\n"
                f"ID: `{plan.take_order_id}`"
            )
        except Exception as e:
            logger.error(f"Take order failed: {e}")
            await self._send(f"⚠️ Тейк не выставлен по `{plan.symbol}`: `{e}`")

        # Стоп-лосс — стоп-маркет reduceOnly
        if plan.sl_price > 0:
            try:
                data = await self.client.place_stop_market_close(
                    plan.symbol, close_side, plan.contracts,
                    plan.sl_price, sl_direction, plan.leverage
                )
                plan.sl_order_id = data.get("orderId", "")
                logger.info(f"SL order: {plan.symbol} @ {plan.sl_price} → {plan.sl_order_id}")
                await self._send(
                    f"🛑 *Стоп-лосс выставлен*\n"
                    f"Символ: `{plan.symbol}`\n"
                    f"Цена стопа: `{plan.sl_price}`\n"
                    f"ID: `{plan.sl_order_id}`"
                )
            except Exception as e:
                logger.error(f"SL order failed: {e}")
                await self._send(
                    f"⚠️ Стоп-лосс не выставлен по `{plan.symbol}`: `{e}`\n"
                    f"Выставьте вручную на: `{plan.sl_price}`"
                )

    # ─────────────────────────────────────────────────────────────────────────
    # ШАГ 3: тейк исполнился — отмена SL + безубыток для остатка
    # ─────────────────────────────────────────────────────────────────────────
    async def _on_take_filled(self, plan: Plan, fill_price: float):
        plan.taken     = True
        close_side     = "sell" if plan.side == "buy" else "buy"
        entry          = plan.entry_price
        remaining      = max(1, plan.contracts - (plan.contracts // 2))

        if plan.side == "buy":
            be_direction = "down"
        else:
            be_direction = "up"

        # Отменяем старый стоп-лосс
        if plan.sl_order_id:
            try:
                await self.client.cancel_order(plan.sl_order_id)
                logger.info(f"Cancelled old SL {plan.sl_order_id}")
            except Exception as e:
                logger.warning(f"Cancel SL failed: {e}")
            plan.sl_order_id = None

        # Новый стоп-маркет в безубыток для оставшихся 50%
        try:
            data = await self.client.place_stop_market_close(
                plan.symbol, close_side, remaining,
                entry, be_direction, plan.leverage
            )
            plan.sl_order_id = data.get("orderId", "")
            logger.info(f"Breakeven order: {plan.symbol} @ {entry} → {plan.sl_order_id}")
            await self._send(
                f"🎯 *Стоп в безубыток выставлен*\n"
                f"Символ: `{plan.symbol}`\n"
                f"Тейк исполнен по: `{fill_price}`\n"
                f"Безубыток на: `{entry}`\n"
                f"Остаток: `{remaining}` контрактов\n"
                f"ID: `{plan.sl_order_id}`"
            )
        except Exception as e:
            logger.error(f"Breakeven failed: {e}")
            await self._send(
                f"⚠️ Безубыток не выставлен по `{plan.symbol}`\n"
                f"Выставьте стоп вручную на: `{entry}`\n"
                f"Ошибка: `{e}`"
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Ручной порез
    # ─────────────────────────────────────────────────────────────────────────
    async def partial_close(self, symbol: str, close_pct: float,
                            reason: str = "Ручной порез") -> Optional[str]:
        position = await self.client.get_position(symbol)
        if not position:
            await self._send(f"⚠️ Нет открытой позиции по `{symbol}`")
            return None

        current_qty = abs(float(position.get("currentQty", 0)))
        if current_qty == 0:
            await self._send(f"⚠️ Позиция по `{symbol}` уже закрыта")
            return None

        close_n    = max(1, round(current_qty * close_pct / 100))
        pos_side   = "buy" if float(position["currentQty"]) > 0 else "sell"
        close_side = "sell" if pos_side == "buy" else "buy"
        lev        = self._plans[symbol].leverage if symbol in self._plans else config.DEFAULT_LEVERAGE

        try:
            info       = await self.client.get_contract_info(symbol)
            multiplier = float(info.get("multiplier", 1))
            cur_price  = await self.client.get_mark_price(symbol)
            close_usdt = close_n * cur_price * multiplier
        except Exception:
            close_usdt = 0.0

        data     = await self.client.place_market_order(symbol, close_side, 0, lev, _contracts=close_n)
        order_id = data.get("orderId", "")

        logger.info(f"Partial close: {symbol} {close_pct}% → {close_n}/{int(current_qty)}c")
        usdt_str = f" (~`{close_usdt:.2f} USDT`)" if close_usdt else ""
        await self._send(
            f"✂️ *{reason}*\n"
            f"Символ: `{symbol}`\n"
            f"Закрыто: `{close_n}` из `{int(current_qty)}` контрактов (`{close_pct}%`){usdt_str}\n"
            f"ID: `{order_id}`"
        )
        return order_id

    # ─────────────────────────────────────────────────────────────────────────
    # WebSocket — исполнение ордеров
    # ─────────────────────────────────────────────────────────────────────────
    async def on_price_update(self, data: dict):
        pass  # мониторинг не нужен — все ордера реальные на бирже

    async def on_order_filled(self, data: dict):
        symbol   = data.get("symbol", "")
        side     = data.get("side", "")
        size     = data.get("filledSize", data.get("size", 0))
        price    = float(data.get("fillPrice", data.get("price", 0)) or 0)
        order_id = data.get("orderId", "")

        usdt_str = ""
        try:
            info       = await self.client.get_contract_info(symbol)
            multiplier = float(info.get("multiplier", 1))
            if price > 0:
                usdt_val = int(size) * price * multiplier
                usdt_str = f"\nОбъём: `~{usdt_val:.2f} USDT`"
        except Exception:
            pass

        await self._send(
            f"✅ *Ордер исполнен*\n"
            f"Символ: `{symbol}`\n"
            f"Сторона: `{'BUY 📈' if side=='buy' else 'SELL 📉'}`\n"
            f"Контрактов: `{size}`{usdt_str}\n"
            f"Цена: `{price}`"
        )

        plan = self._plans.get(symbol)
        if not plan:
            return

        logger.info(f"Checking order match: event_id={order_id} "
                    f"entry_id={plan.entry_order_id} take_id={plan.take_order_id} "
                    f"filled={plan.filled} taken={plan.taken}")

        # Сравниваем orderId — WS может слать укороченный вариант
        def ids_match(a: str, b: str) -> bool:
            if not a or not b:
                return False
            return a == b or a.startswith(b) or b.startswith(a)

        # Лимитный ордер на вход исполнился → выставляем тейк + SL
        if ids_match(plan.entry_order_id, order_id) and not plan.filled:
            plan.filled = True
            logger.info(f"Entry filled: {symbol} @ {price}")
            await self._on_entry_filled(plan, price)

        # Тейк-профит исполнился → отменяем SL + безубыток
        elif ids_match(plan.take_order_id, order_id) and not plan.taken:
            logger.info(f"Take filled: {symbol} @ {price}")
            await self._on_take_filled(plan, price)

    async def on_trailing_stop_triggered(self, data: dict):
        symbol = data.get("symbol", "")
        size   = data.get("size", "")
        await self._send(
            f"🛑 *Стоп сработал*\n"
            f"Символ: `{symbol}`\nЗакрыто: `{size}` контрактов"
        )
        if symbol in self._plans:
            del self._plans[symbol]

    async def on_position_opened(self, data: dict):
        symbol = data.get("symbol", "")
        qty    = float(data.get("currentQty", 0))
        price  = data.get("avgEntryPrice", "N/A")
        side   = "LONG 📈" if qty > 0 else "SHORT 📉"
        await self._send(
            f"🟢 *Позиция открыта*\n"
            f"Символ: `{symbol}`\n"
            f"Направление: `{side}`\n"
            f"Объём: `{abs(qty)}` контрактов\n"
            f"Цена входа: `{price}`"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Отмена
    # ─────────────────────────────────────────────────────────────────────────
    async def cancel_order(self, order_id: str) -> bool:
        try:
            await self.client.cancel_order(order_id)
            await self._send(f"🗑 Ордер `{order_id}` отменён")
            return True
        except Exception as e:
            await self._send(f"⚠️ Не удалось отменить `{order_id}`: {e}")
            return False

    async def cancel_all(self, symbol: str) -> bool:
        try:
            await self.client.cancel_all_orders(symbol)
            if symbol in self._plans:
                del self._plans[symbol]
            await self._send(f"🗑 Все ордера по `{symbol}` отменены")
            return True
        except Exception as e:
            await self._send(f"⚠️ Ошибка отмены: {e}")
            return False