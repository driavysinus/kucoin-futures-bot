"""
order_manager.py
High-level trading logic:
  • Place limit / market orders with leverage
  • Place native KuCoin trailing-stop orders
  • Auto partial-close when price moves N% in position direction
  • Manual partial close by %
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Optional, Callable
from loguru import logger

from kucoin_client import KuCoinFuturesClient
import config


@dataclass
class TrailingStopConfig:
    """Tracks an active trailing-stop + auto-close rule per symbol."""
    symbol:           str
    side:             str          # "buy" or "sell" (direction of trade)
    entry_price:      float
    size:             float        # total position size in contracts
    usdt_size:        float        # original position size in USDT
    callback_rate:    float        # trailing-stop callback %
    profit_trigger:   float        # % move to fire auto partial-close
    partial_close_pct: float       # % of position to close
    order_id:         Optional[str] = None   # native trailing-stop order id
    auto_closed:      bool = False  # already fired once per trigger level


class OrderManager:
    def __init__(self, client: KuCoinFuturesClient,
                 notify: Callable = None):
        self.client   = client
        self._notify  = notify or (lambda msg: None)   # async callable
        self._trailing: dict[str, TrailingStopConfig] = {}
        self._leverage: dict[str, int] = {}             # symbol → leverage

    def set_leverage(self, symbol: str, leverage: int):
        self._leverage[symbol] = leverage

    def get_leverage(self, symbol: str) -> int:
        return self._leverage.get(symbol, config.DEFAULT_LEVERAGE)

    # ── Notifications ─────────────────────────────────────────────────────────
    async def _send(self, msg: str):
        try:
            if asyncio.iscoroutinefunction(self._notify):
                await self._notify(msg)
            else:
                self._notify(msg)
        except Exception as e:
            logger.error(f"Notify error: {e}")

    # ── Limit order ───────────────────────────────────────────────────────────
    async def place_limit_order(self, symbol: str, side: str,
                                usdt_amount: float, price: float,
                                leverage: int = None) -> str:
        """
        Place a limit order. usdt_amount is in USDT — converted to contracts automatically.
        """
        lev = leverage or self.get_leverage(symbol)
        contracts, ref_price, multiplier = await self.client.usdt_to_contracts(
            symbol, usdt_amount, price
        )
        actual_usdt = contracts * price * multiplier

        oid  = uuid.uuid4().hex[:16]
        data = await self.client.place_limit_order(
            symbol, side, contracts, price, lev, client_oid=oid
        )
        order_id = data.get("orderId", oid)
        logger.info(f"Limit order placed: {symbol} {side} {contracts} contracts "
                    f"(~{actual_usdt:.2f} USDT) @{price} → {order_id}")
        await self._send(
            f"📋 *Лимитный ордер выставлен*\n"
            f"Символ: `{symbol}`\n"
            f"Направление: `{'LONG 📈' if side=='buy' else 'SHORT 📉'}`\n"
            f"Размер: `{actual_usdt:.2f} USDT` → `{contracts}` контрактов\n"
            f"Цена: `{price}`\n"
            f"Плечо: `{lev}x`\n"
            f"ID: `{order_id}`"
        )
        return order_id

    # ── Market order ──────────────────────────────────────────────────────────
    async def place_market_order(self, symbol: str, side: str,
                                 usdt_amount: float, leverage: int = None,
                                 _contracts: int = None) -> str:
        """
        Place a market order.
        Pass usdt_amount in USDT (converted automatically).
        Internal callers (partial_close) may pass _contracts directly to skip conversion.
        """
        lev = leverage or self.get_leverage(symbol)
        if _contracts is not None:
            contracts = _contracts
        else:
            contracts, _, _ = await self.client.usdt_to_contracts(symbol, usdt_amount)

        data     = await self.client.place_market_order(symbol, side, contracts, lev)
        order_id = data.get("orderId", "")
        logger.info(f"Market order placed: {symbol} {side} {contracts} contracts → {order_id}")
        return order_id

    # ── Trailing-stop order ───────────────────────────────────────────────────
    async def place_trailing_stop(self, symbol: str, side: str,
                                  usdt_amount: float, callback_rate: float,
                                  entry_price: float,
                                  leverage: int = None,
                                  profit_trigger: float = None,
                                  partial_close_pct: float = None) -> str:
        """
        Place a native KuCoin trailing-stop and register auto-partial-close rule.
        usdt_amount: position size in USDT (converted to contracts automatically)
        side: direction of the position ('buy'=long, 'sell'=short)
        """
        lev       = leverage or self.get_leverage(symbol)
        stop_side = "sell" if side == "buy" else "buy"

        contracts, ref_price, multiplier = await self.client.usdt_to_contracts(
            symbol, usdt_amount, entry_price if entry_price > 0 else None
        )
        actual_usdt = contracts * (entry_price or ref_price) * multiplier

        data     = await self.client.place_trailing_stop_order(
            symbol, stop_side, contracts, callback_rate, lev
        )
        order_id = data.get("orderId", "")

        cfg = TrailingStopConfig(
            symbol=symbol,
            side=side,
            entry_price=entry_price or ref_price,
            size=contracts,
            usdt_size=usdt_amount,
            callback_rate=callback_rate,
            profit_trigger=profit_trigger or config.DEFAULT_PROFIT_TRIGGER_PCT,
            partial_close_pct=partial_close_pct or config.DEFAULT_PARTIAL_CLOSE_PCT,
            order_id=order_id,
        )
        self._trailing[symbol] = cfg

        logger.info(f"Trailing-stop placed: {symbol} {stop_side} {contracts} contracts "
                    f"(~{actual_usdt:.2f} USDT) callback={callback_rate}% → {order_id}")
        await self._send(
            f"🔁 *Трейлинг-стоп выставлен*\n"
            f"Символ: `{symbol}`\n"
            f"Позиция: `{'LONG 📈' if side=='buy' else 'SHORT 📉'}`\n"
            f"Размер: `{actual_usdt:.2f} USDT` → `{contracts}` контрактов\n"
            f"Отступ (callback): `{callback_rate}%`\n"
            f"Триггер пореза: `+{cfg.profit_trigger}%` от входа\n"
            f"Порез: `{cfg.partial_close_pct}%` позиции\n"
            f"ID: `{order_id}`"
        )
        return order_id

    # ── Partial close ─────────────────────────────────────────────────────────
    async def partial_close(self, symbol: str, close_pct: float,
                            reason: str = "Ручной порез") -> Optional[str]:
        """Close close_pct % of current position at market price."""
        position = await self.client.get_position(symbol)
        if not position:
            await self._send(f"⚠️ Нет открытой позиции по `{symbol}`")
            return None

        current_qty = abs(float(position.get("currentQty", 0)))
        if current_qty == 0:
            await self._send(f"⚠️ Позиция по `{symbol}` уже закрыта")
            return None

        close_contracts = max(1, round(current_qty * close_pct / 100))
        pos_side        = "buy" if float(position["currentQty"]) > 0 else "sell"
        close_side      = "sell" if pos_side == "buy" else "buy"
        lev             = self.get_leverage(symbol)

        # Estimate USDT value of the contracts being closed
        try:
            info       = await self.client.get_contract_info(symbol)
            multiplier = float(info.get("multiplier", 1))
            cur_price  = await self.client.get_mark_price(symbol)
            close_usdt = close_contracts * cur_price * multiplier
        except Exception:
            close_usdt = 0.0
            multiplier = 1.0

        # Use _contracts to skip re-conversion (we already have exact count)
        data     = await self.client.place_market_order(
            symbol, close_side, 0, lev, _contracts=close_contracts
        )
        order_id = data.get("orderId", "")

        usdt_str = f" (~`{close_usdt:.2f} USDT`)" if close_usdt else ""
        logger.info(f"Partial close: {symbol} {close_pct}% → "
                    f"{close_contracts}/{int(current_qty)} contracts {order_id}")
        await self._send(
            f"✂️ *{reason}*\n"
            f"Символ: `{symbol}`\n"
            f"Закрыто: `{close_contracts}` из `{int(current_qty)}` контрактов "
            f"(`{close_pct}%`){usdt_str}\n"
            f"ID ордера: `{order_id}`"
        )

        # update trailing config size
        if symbol in self._trailing:
            self._trailing[symbol].size -= close_contracts

        return order_id

    # ── Price tick handler (called by monitor) ────────────────────────────────
    async def on_price_update(self, data: dict):
        """
        Called on every ticker update from WebSocket.
        Checks if auto partial-close trigger is hit.
        """
        symbol = data["symbol"]
        price  = data["price"]
        cfg    = self._trailing.get(symbol)
        if not cfg or cfg.auto_closed:
            return

        entry = cfg.entry_price
        if entry <= 0:
            return

        move_pct = ((price - entry) / entry) * 100
        if cfg.side == "sell":
            move_pct = -move_pct   # for short: price drop = profit

        if move_pct >= cfg.profit_trigger:
            logger.info(f"Auto partial-close triggered: {symbol} "
                        f"+{move_pct:.2f}% >= {cfg.profit_trigger}%")
            cfg.auto_closed = True   # prevent repeated fires
            await self.partial_close(
                symbol, cfg.partial_close_pct,
                reason=f"Автопорез +{move_pct:.1f}% от входа"
            )
            # reset trigger for next level (optional: raise trigger)
            cfg.profit_trigger += cfg.profit_trigger   # double next trigger
            cfg.auto_closed = False

    # ── Cancel ────────────────────────────────────────────────────────────────
    async def cancel_order(self, order_id: str) -> bool:
        try:
            await self.client.cancel_order(order_id)
            await self._send(f"🗑 Ордер `{order_id}` отменён")
            return True
        except Exception as e:
            await self._send(f"⚠️ Не удалось отменить ордер `{order_id}`: {e}")
            return False

    async def cancel_all(self, symbol: str) -> bool:
        try:
            await self.client.cancel_all_orders(symbol)
            if symbol in self._trailing:
                del self._trailing[symbol]
            await self._send(f"🗑 Все ордера по `{symbol}` отменены")
            return True
        except Exception as e:
            await self._send(f"⚠️ Ошибка отмены ордеров: {e}")
            return False

    # ── Event handlers from monitor ───────────────────────────────────────────
    async def on_order_filled(self, data: dict):
        symbol    = data.get("symbol", "")
        side      = data.get("side", "")
        size      = data.get("filledSize", data.get("size", 0))
        price     = float(data.get("fillPrice", data.get("price", 0)) or 0)
        otype     = data.get("type", "")
        type_label = "Трейлинг-стоп" if "trailing" in otype else "Лимитный"

        # Estimate USDT value
        usdt_str = ""
        try:
            info       = await self.client.get_contract_info(symbol)
            multiplier = float(info.get("multiplier", 1))
            if price > 0:
                usdt_val = int(size) * price * multiplier
                usdt_str = f"\nОбъём USDT: `~{usdt_val:.2f} USDT`"
        except Exception:
            pass

        await self._send(
            f"✅ *{type_label} ордер исполнен*\n"
            f"Символ: `{symbol}`\n"
            f"Сторона: `{'BUY 📈' if side=='buy' else 'SELL 📉'}`\n"
            f"Контрактов: `{size}`{usdt_str}\n"
            f"Цена исполнения: `{price}`"
        )

    async def on_trailing_stop_triggered(self, data: dict):
        symbol = data.get("symbol", "")
        size   = data.get("size", "")
        await self._send(
            f"🛑 *Трейлинг-стоп сработал!*\n"
            f"Символ: `{symbol}`\n"
            f"Закрыто: `{size}` контрактов"
        )
        if symbol in self._trailing:
            del self._trailing[symbol]

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
            f"Средняя цена входа: `{price}`"
        )
