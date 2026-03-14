"""
order_manager.py

Команды и логика:

/open SYMBOL SIDE USDT PRICE [CB%] [TRIG%] [CLOSE%] [LEV]
    → Лимитный ордер на вход по вашей цене PRICE
    → После исполнения: авто трейлинг-стоп CB%
    → При движении +TRIG% → порез CLOSE% позиции + стоп в безубыток

/stop SYMBOL SIDE USDT PRICE [CB%] [TRIG%] [CLOSE%] [LEV]
    → Стоп-маркет ордер на вход (выставляется заблаговременно)
    → buy:  активируется когда цена ПОДНИМАЕТСЯ до PRICE (покупка на пробой)
    → sell: активируется когда цена ПАДАЕТ до PRICE (продажа на пробой)
    → После исполнения: та же авто-логика

/trailing SYMBOL SIDE USDT CB% ACTIVATE [TRIG%] [CLOSE%] [LEV]
    → Трейлинг-стоп на уже открытую позицию (reduceOnly)
    → При движении +TRIG% → порез CLOSE% + стоп в безубыток

/close SYMBOL [PCT%]
    → Ручной порез позиции
"""

import asyncio
from dataclasses import dataclass
from typing import Optional, Callable
from loguru import logger

from kucoin_client import KuCoinFuturesClient
import config


@dataclass
class PositionPlan:
    symbol:            str
    side:              str        # направление позиции: "buy"=long, "sell"=short
    entry_price:       float      # цена ордера на вход
    contracts:         int
    usdt_amount:       float
    callback_rate:     float      # % callback трейлинг-стопа
    profit_trigger:    float      # % профита для автопореза
    partial_close_pct: float      # % пореза
    leverage:          int
    sl_price:          float = 0.0        # стоп-лосс цена (0 = не задан)
    entry_order_id:    Optional[str] = None
    trailing_order_id: Optional[str] = None
    sl_order_id:       Optional[str] = None
    filled:            bool = False   # ордер на вход исполнен
    partial_done:      bool = False   # порез уже был
    breakeven_set:     bool = False   # стоп уже в безубытке


class OrderManager:
    def __init__(self, client: KuCoinFuturesClient, notify: Callable = None):
        self.client   = client
        self._notify  = notify or (lambda msg: None)
        self._plans:    dict[str, PositionPlan] = {}
        self._leverage: dict[str, int] = {}

    def set_leverage(self, symbol: str, leverage: int):
        self._leverage[symbol] = leverage

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
    # /open — лимитный ордер на вход
    # ─────────────────────────────────────────────────────────────────────────
    async def place_limit_order(self, symbol: str, side: str,
                                usdt_amount: float, price: float,
                                leverage: int = None,
                                callback_rate: float = None,
                                profit_trigger: float = None,
                                partial_close_pct: float = None,
                                sl_price: float = 0.0) -> str:
        lev  = leverage or self.get_leverage(symbol)
        cb   = callback_rate    or config.DEFAULT_TRAILING_STOP_PCT
        trig = profit_trigger   or config.DEFAULT_PROFIT_TRIGGER_PCT
        pct  = partial_close_pct or config.DEFAULT_PARTIAL_CLOSE_PCT

        contracts, _, multiplier = await self.client.usdt_to_contracts(symbol, usdt_amount, price)
        actual_usdt = contracts * price * multiplier

        data     = await self.client.place_limit_order(symbol, side, contracts, price, lev)
        order_id = data.get("orderId", "")

        self._plans[symbol] = PositionPlan(
            symbol=symbol, side=side, entry_price=price,
            contracts=contracts, usdt_amount=actual_usdt,
            callback_rate=cb, profit_trigger=trig, partial_close_pct=pct,
            leverage=lev, sl_price=sl_price, entry_order_id=order_id,
        )
        logger.info(f"Limit order: {symbol} {side} {contracts}c @{price} SL={sl_price} → {order_id}")

        sl_line = f"\n  🛑 Стоп-лосс: `{sl_price}`" if sl_price > 0 else ""
        await self._send(
            f"📋 *Лимитный ордер выставлен*\n"
            f"Символ: `{symbol}`\n"
            f"Направление: `{'LONG 📈' if side=='buy' else 'SHORT 📉'}`\n"
            f"Размер: `{actual_usdt:.2f} USDT` → `{contracts}` контрактов\n"
            f"Цена входа: `{price}`  |  Плечо: `{lev}x`\n"
            f"После исполнения автоматически:{sl_line}\n"
            f"  🔁 Трейлинг-стоп callback `{cb}%`\n"
            f"  ✂️ Порез `{pct}%` при профите `+{trig}%`\n"
            f"  🎯 Стоп в безубыток после пореза\n"
            f"ID: `{order_id}`"
        )
        return order_id

    # ─────────────────────────────────────────────────────────────────────────
    # /stop — стоп-маркет ордер на вход (заблаговременно)
    # buy:  цена поднимается до PRICE → покупаем (пробой вверх)
    # sell: цена падает до PRICE → продаём (пробой вниз)
    # ─────────────────────────────────────────────────────────────────────────
    async def place_stop_entry(self, symbol: str, side: str,
                               usdt_amount: float, price: float,
                               leverage: int = None,
                               callback_rate: float = None,
                               profit_trigger: float = None,
                               partial_close_pct: float = None,
                               sl_price: float = 0.0) -> str:
        lev  = leverage or self.get_leverage(symbol)
        cb   = callback_rate    or config.DEFAULT_TRAILING_STOP_PCT
        trig = profit_trigger   or config.DEFAULT_PROFIT_TRIGGER_PCT
        pct  = partial_close_pct or config.DEFAULT_PARTIAL_CLOSE_PCT

        contracts, _, multiplier = await self.client.usdt_to_contracts(symbol, usdt_amount, price)
        actual_usdt = contracts * price * multiplier
        stop_direction = "up" if side == "buy" else "down"

        data     = await self.client.place_stop_market_entry(
            symbol, side, contracts, price, stop_direction, lev
        )
        order_id = data.get("orderId", "")

        self._plans[symbol] = PositionPlan(
            symbol=symbol, side=side, entry_price=price,
            contracts=contracts, usdt_amount=actual_usdt,
            callback_rate=cb, profit_trigger=trig, partial_close_pct=pct,
            leverage=lev, sl_price=sl_price, entry_order_id=order_id,
        )
        logger.info(f"Stop entry: {symbol} {side} {contracts}c @{price} SL={sl_price} → {order_id}")

        sl_line = f"\n  🛑 Стоп-лосс: `{sl_price}`" if sl_price > 0 else ""
        await self._send(
            f"🎯 *Стоп-ордер на вход выставлен*\n"
            f"Символ: `{symbol}`\n"
            f"Направление: `{'LONG 📈' if side=='buy' else 'SHORT 📉'}`\n"
            f"Размер: `{actual_usdt:.2f} USDT` → `{contracts}` контрактов\n"
            f"Цена активации: `{price}`\n"
            f"{'📈 Вход при росте до' if side=='buy' else '📉 Вход при падении до'} `{price}`\n"
            f"Плечо: `{lev}x`\n"
            f"После исполнения автоматически:{sl_line}\n"
            f"  🔁 Трейлинг-стоп callback `{cb}%`\n"
            f"  ✂️ Порез `{pct}%` при профите `+{trig}%`\n"
            f"  🎯 Стоп в безубыток после пореза\n"
            f"ID: `{order_id}`"
        )
        return order_id

    # ─────────────────────────────────────────────────────────────────────────
    # /trailing — трейлинг-стоп на уже открытую позицию (reduceOnly)
    # ─────────────────────────────────────────────────────────────────────────
    async def place_trailing_stop(self, symbol: str, side: str,
                                  usdt_amount: float, callback_rate: float,
                                  activate_price: float,
                                  leverage: int = None,
                                  profit_trigger: float = None,
                                  partial_close_pct: float = None) -> str:
        lev       = leverage or self.get_leverage(symbol)
        trig      = profit_trigger    or config.DEFAULT_PROFIT_TRIGGER_PCT
        pct       = partial_close_pct or config.DEFAULT_PARTIAL_CLOSE_PCT
        stop_side = "sell" if side == "buy" else "buy"

        # Проверяем позицию
        pos = await self.client.get_position(symbol)
        if not pos:
            raise ValueError(
                f"Нет открытой позиции по {symbol}.\n"
                f"Для заблаговременного входа используйте `/stop`"
            )

        pos_qty    = float(pos.get("currentQty", 0))
        real_entry = float(pos.get("avgEntryPrice", activate_price))
        real_contracts = abs(int(pos_qty))

        contracts, _, multiplier = await self.client.usdt_to_contracts(
            symbol, usdt_amount, activate_price
        )
        contracts   = min(contracts, real_contracts)
        actual_usdt = contracts * activate_price * multiplier

        data     = await self.client.place_trailing_stop_order(
            symbol, stop_side, contracts, callback_rate, lev,
            stop_price=activate_price
        )
        order_id = data.get("orderId", "")

        self._plans[symbol] = PositionPlan(
            symbol=symbol, side=side,
            entry_price=real_entry, contracts=contracts, usdt_amount=actual_usdt,
            callback_rate=callback_rate, profit_trigger=trig, partial_close_pct=pct,
            leverage=lev, trailing_order_id=order_id, filled=True,
        )
        logger.info(f"Trailing stop: {symbol} {stop_side} {contracts}c activate={activate_price} → {order_id}")
        await self._send(
            f"🔁 *Трейлинг-стоп выставлен*\n"
            f"Символ: `{symbol}`\n"
            f"Позиция: `{'LONG 📈' if side=='buy' else 'SHORT 📉'}`\n"
            f"Размер: `{contracts}` контрактов (~`{actual_usdt:.2f} USDT`)\n"
            f"Цена входа (факт): `{real_entry}`\n"
            f"Цена активации стопа: `{activate_price}`\n"
            f"Callback: `{callback_rate}%`\n"
            f"Триггер пореза: `+{trig}%` → порез `{pct}%`\n"
            f"После пореза: 🎯 стоп в безубыток\n"
            f"ID: `{order_id}`"
        )
        return order_id

    # ─────────────────────────────────────────────────────────────────────────
    # Внутренний: авто трейлинг-стоп после исполнения ордера на вход
    # ─────────────────────────────────────────────────────────────────────────
    async def _auto_place_trailing_stop(self, plan: PositionPlan, fill_price: float):
        stop_side = "sell" if plan.side == "buy" else "buy"

        # Лонг: стоп ниже цены входа на callback% (sell-стоп, активируется при падении)
        # Шорт: стоп выше цены входа на callback% (buy-стоп, активируется при росте)
        cb = plan.callback_rate / 100
        if plan.side == "buy":
            activate_price = round(fill_price * (1 - cb), 8)
        else:
            activate_price = round(fill_price * (1 + cb), 8)

        try:
            data = await self.client.place_trailing_stop_order(
                plan.symbol, stop_side, plan.contracts,
                plan.callback_rate, plan.leverage,
                stop_price=activate_price
            )
            plan.trailing_order_id = data.get("orderId", "")
            logger.info(f"Auto trailing stop: {plan.symbol} activate={activate_price} → {plan.trailing_order_id}")
            await self._send(
                f"🔁 *Трейлинг-стоп выставлен автоматически*\n"
                f"Символ: `{plan.symbol}`\n"
                f"Цена входа: `{fill_price}`\n"
                f"Цена активации стопа: `{activate_price}`\n"
                f"Callback: `{plan.callback_rate}%`\n"
                f"ID: `{plan.trailing_order_id}`"
            )
        except Exception as e:
            logger.error(f"Auto trailing stop failed: {e}")
            await self._send(
                f"⚠️ Не удалось выставить трейлинг-стоп по `{plan.symbol}`\n"
                f"Выставьте вручную: `/trailing {plan.symbol} {'buy' if plan.side=='buy' else 'sell'} ...`\n"
                f"Ошибка: `{e}`"
            )
        # Стоп-лосс выставляем параллельно если задан
        if plan.sl_price > 0:
            await self._auto_place_stop_loss(plan)

    async def _auto_place_stop_loss(self, plan: PositionPlan):
        """Выставляет стоп-лосс сразу после открытия позиции."""
        stop_side = "sell" if plan.side == "buy" else "buy"
        try:
            data = await self.client.place_stop_limit_order(
                plan.symbol, stop_side, plan.contracts,
                plan.sl_price, plan.sl_price, plan.leverage
            )
            plan.sl_order_id = data.get("orderId", "")
            logger.info(f"Auto SL placed: {plan.symbol} @ {plan.sl_price} → {plan.sl_order_id}")
            await self._send(
                f"🛑 *Стоп-лосс выставлен автоматически*\n"
                f"Символ: `{plan.symbol}`\n"
                f"Цена стопа: `{plan.sl_price}`\n"
                f"ID: `{plan.sl_order_id}`"
            )
        except Exception as e:
            logger.error(f"Auto SL failed: {e}")
            await self._send(
                f"⚠️ Не удалось выставить стоп-лосс по `{plan.symbol}`\n"
                f"Выставьте вручную стоп на цену: `{plan.sl_price}`\n"
                f"Ошибка: `{e}`"
            )

    # ─────────────────────────────────────────────────────────────────────────
    # Порез позиции
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

        close_contracts = max(1, round(current_qty * close_pct / 100))
        pos_side   = "buy" if float(position["currentQty"]) > 0 else "sell"
        close_side = "sell" if pos_side == "buy" else "buy"
        lev        = self.get_leverage(symbol)

        try:
            info       = await self.client.get_contract_info(symbol)
            multiplier = float(info.get("multiplier", 1))
            cur_price  = await self.client.get_mark_price(symbol)
            close_usdt = close_contracts * cur_price * multiplier
        except Exception:
            close_usdt = 0.0

        data     = await self.client.place_market_order(
            symbol, close_side, 0, lev, _contracts=close_contracts
        )
        order_id = data.get("orderId", "")

        usdt_str = f" (~`{close_usdt:.2f} USDT`)" if close_usdt else ""
        logger.info(f"Partial close: {symbol} {close_pct}% → {close_contracts}/{int(current_qty)}c")
        await self._send(
            f"✂️ *{reason}*\n"
            f"Символ: `{symbol}`\n"
            f"Закрыто: `{close_contracts}` из `{int(current_qty)}` контрактов "
            f"(`{close_pct}%`){usdt_str}\n"
            f"ID: `{order_id}`"
        )

        if symbol in self._plans:
            self._plans[symbol].contracts -= close_contracts

        return order_id

    # ─────────────────────────────────────────────────────────────────────────
    # Стоп в безубыток
    # ─────────────────────────────────────────────────────────────────────────
    async def _move_to_breakeven(self, plan: PositionPlan):
        symbol    = plan.symbol
        entry     = plan.entry_price
        stop_side = "sell" if plan.side == "buy" else "buy"
        lev       = plan.leverage

        if plan.trailing_order_id:
            try:
                await self.client.cancel_order(plan.trailing_order_id)
            except Exception as e:
                logger.warning(f"Could not cancel trailing stop: {e}")

        # Отменяем и старый стоп-лосс
        if plan.sl_order_id:
            try:
                await self.client.cancel_order(plan.sl_order_id)
                plan.sl_order_id = None
            except Exception as e:
                logger.warning(f"Could not cancel SL: {e}")

        try:
            pos       = await self.client.get_position(symbol)
            remaining = abs(float(pos.get("currentQty", plan.contracts))) if pos else plan.contracts
        except Exception:
            remaining = plan.contracts

        if remaining <= 0:
            return

        try:
            data   = await self.client.place_stop_limit_order(
                symbol, stop_side, remaining, entry, entry, lev
            )
            new_id = data.get("orderId", "")
            plan.trailing_order_id = new_id
            plan.breakeven_set     = True
            logger.info(f"Breakeven stop: {symbol} @ {entry} → {new_id}")
            await self._send(
                f"🎯 *Стоп-лосс перенесён в безубыток*\n"
                f"Символ: `{symbol}`\n"
                f"Стоп на цене входа: `{entry}`\n"
                f"Контрактов: `{remaining}`\n"
                f"ID: `{new_id}`"
            )
        except Exception as e:
            logger.error(f"Breakeven stop failed: {e}")
            await self._send(
                f"⚠️ *Не удалось выставить безубыток автоматически*\n"
                f"Выставьте стоп вручную на цену входа: `{entry}`\n"
                f"Ошибка: `{e}`"
            )

    # ─────────────────────────────────────────────────────────────────────────
    # WebSocket — цена (мониторинг триггера пореза)
    # ─────────────────────────────────────────────────────────────────────────
    async def on_price_update(self, data: dict):
        symbol = data["symbol"]
        price  = float(data["price"])
        plan   = self._plans.get(symbol)

        if not plan or not plan.filled or plan.partial_done:
            return
        if plan.entry_price <= 0:
            return

        move_pct = ((price - plan.entry_price) / plan.entry_price) * 100
        if plan.side == "sell":
            move_pct = -move_pct

        if move_pct >= plan.profit_trigger:
            logger.info(f"Profit trigger: {symbol} +{move_pct:.2f}% → порез {plan.partial_close_pct}%")
            plan.partial_done = True
            await self.partial_close(symbol, plan.partial_close_pct,
                                     reason=f"Автопорез +{move_pct:.1f}% от входа")
            await self._move_to_breakeven(plan)

    # ─────────────────────────────────────────────────────────────────────────
    # WebSocket — исполнение ордера
    # ─────────────────────────────────────────────────────────────────────────
    async def on_order_filled(self, data: dict):
        symbol   = data.get("symbol", "")
        side     = data.get("side", "")
        size     = data.get("filledSize", data.get("size", 0))
        price    = float(data.get("fillPrice", data.get("price", 0)) or 0)
        order_id = data.get("orderId", "")
        otype    = data.get("type", "")
        label    = "Трейлинг-стоп" if "trailing" in otype else "Ордер"

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
            f"✅ *{label} исполнен*\n"
            f"Символ: `{symbol}`\n"
            f"Сторона: `{'BUY 📈' if side=='buy' else 'SELL 📉'}`\n"
            f"Контрактов: `{size}`{usdt_str}\n"
            f"Цена: `{price}`"
        )

        # Если это наш ордер на вход — запускаем авто трейлинг-стоп
        plan = self._plans.get(symbol)
        if plan and plan.entry_order_id == order_id and not plan.filled:
            plan.filled      = True
            plan.entry_price = price
            logger.info(f"Entry filled: {symbol} @ {price} — launching auto trailing stop")
            await self._auto_place_trailing_stop(plan, price)

    async def on_trailing_stop_triggered(self, data: dict):
        symbol = data.get("symbol", "")
        size   = data.get("size", "")
        await self._send(
            f"🛑 *Трейлинг-стоп сработал*\n"
            f"Символ: `{symbol}`\n"
            f"Закрыто: `{size}` контрактов"
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