"""
order_manager.py

Команда: /open SYMBOL SIDE USDT PRICE SL TRIG% LEV

После исполнения лимитки автоматически:
  1. Стоп-лосс на SL
  2. Мониторинг цены — при движении +TRIG% → порез 50% → стоп в безубыток
"""

import asyncio
from dataclasses import dataclass
from typing import Optional, Callable
from loguru import logger

from kucoin_client import KuCoinFuturesClient
import config


@dataclass
class Plan:
    symbol:        str
    side:          str       # "buy" / "sell"
    entry_price:   float     # цена исполнения
    contracts:     int
    sl_price:      float     # стоп-лосс (0 = не задан)
    trim_pct:      float     # % движения для пореза
    leverage:      int
    entry_order_id: Optional[str] = None
    sl_order_id:    Optional[str] = None
    filled:  bool = False
    trimmed: bool = False    # порез уже был


class OrderManager:
    def __init__(self, client: KuCoinFuturesClient, notify: Callable = None):
        self.client  = client
        self._notify = notify or (lambda msg: None)
        self._plans: dict[str, Plan] = {}
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
    # /open
    # ─────────────────────────────────────────────────────────────────────────
    async def place_limit_order(self, symbol: str, side: str,
                                usdt_amount: float, price: float,
                                sl_price: float, trim_pct: float,
                                leverage: int) -> str:

        contracts, _, multiplier = await self.client.usdt_to_contracts(symbol, usdt_amount, price)
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
            f"  ✂️ Порез `50%` при движении `+{trim_pct}%`\n"
            f"  🎯 Безубыток после пореза\n"
            f"ID: `{order_id}`"
        )
        return order_id

    # ─────────────────────────────────────────────────────────────────────────
    # Авто-действия после исполнения лимитки
    # ─────────────────────────────────────────────────────────────────────────
    async def _on_entry_filled(self, plan: Plan, fill_price: float):
        plan.entry_price = fill_price
        stop_side = "sell" if plan.side == "buy" else "buy"

        if plan.sl_price > 0:
            try:
                data = await self.client.place_stop_limit_order(
                    plan.symbol, stop_side, plan.contracts,
                    plan.sl_price, plan.sl_price, plan.leverage
                )
                plan.sl_order_id = data.get("orderId", "")
                logger.info(f"Auto SL: {plan.symbol} @ {plan.sl_price} → {plan.sl_order_id}")
                await self._send(
                    f"🛑 *Стоп-лосс выставлен автоматически*\n"
                    f"Символ: `{plan.symbol}`\n"
                    f"Цена стопа: `{plan.sl_price}`\n"
                    f"ID: `{plan.sl_order_id}`"
                )
            except Exception as e:
                logger.error(f"Auto SL failed: {e}")
                await self._send(
                    f"⚠️ Стоп-лосс не выставлен по `{plan.symbol}`: `{e}`\n"
                    f"Выставьте вручную на: `{plan.sl_price}`"
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

        if symbol in self._plans:
            self._plans[symbol].contracts = max(0, self._plans[symbol].contracts - close_n)

        return order_id

    # ─────────────────────────────────────────────────────────────────────────
    # Перенос в безубыток
    # ─────────────────────────────────────────────────────────────────────────
    async def _move_to_breakeven(self, plan: Plan):
        symbol    = plan.symbol
        entry     = plan.entry_price
        stop_side = "sell" if plan.side == "buy" else "buy"

        # Отменяем старый SL
        if plan.sl_order_id:
            try:
                await self.client.cancel_order(plan.sl_order_id)
                logger.info(f"Cancelled SL {plan.sl_order_id}")
            except Exception as e:
                logger.warning(f"Cancel SL failed: {e}")
            plan.sl_order_id = None

        # Актуальный размер позиции
        try:
            pos       = await self.client.get_position(symbol)
            remaining = abs(float(pos.get("currentQty", plan.contracts))) if pos else plan.contracts
        except Exception:
            remaining = plan.contracts

        if remaining <= 0:
            return

        try:
            data   = await self.client.place_stop_limit_order(
                symbol, stop_side, remaining, entry, entry, plan.leverage
            )
            plan.sl_order_id = data.get("orderId", "")
            logger.info(f"Breakeven: {symbol} @ {entry} → {plan.sl_order_id}")
            await self._send(
                f"🎯 *Стоп-лосс перенесён в безубыток*\n"
                f"Символ: `{symbol}`\n"
                f"Стоп на цене входа: `{entry}`\n"
                f"Контрактов: `{remaining}`\n"
                f"ID: `{plan.sl_order_id}`"
            )
        except Exception as e:
            logger.error(f"Breakeven failed: {e}")
            await self._send(
                f"⚠️ Безубыток не выставлен автоматически\n"
                f"Выставьте стоп вручную на: `{entry}`\n"
                f"Ошибка: `{e}`"
            )

    # ─────────────────────────────────────────────────────────────────────────
    # WebSocket — мониторинг цены для пореза
    # ─────────────────────────────────────────────────────────────────────────
    async def on_price_update(self, data: dict):
        symbol = data["symbol"]
        price  = float(data["price"])
        plan   = self._plans.get(symbol)

        if not plan or not plan.filled or plan.trimmed:
            return
        if plan.entry_price <= 0:
            return

        move_pct = ((price - plan.entry_price) / plan.entry_price) * 100
        if plan.side == "sell":
            move_pct = -move_pct

        if move_pct >= plan.trim_pct:
            logger.info(f"TRIM triggered: {symbol} +{move_pct:.2f}% >= {plan.trim_pct}%")
            plan.trimmed = True
            await self.partial_close(symbol, 50.0,
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

        plan = self._plans.get(symbol)
        if plan and plan.entry_order_id == order_id and not plan.filled:
            plan.filled = True
            logger.info(f"Entry filled: {symbol} @ {price}")
            await self._on_entry_filled(plan, price)

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