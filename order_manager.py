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
    side:           str
    entry_price:    float
    contracts:      int
    sl_price:       float
    trim_pct:       float
    leverage:       int
    entry_order_id: Optional[str] = None
    sl_order_id:    Optional[str] = None
    take_order_id:  Optional[str] = None   # тейк 1 — 50%
    take2_order_id: Optional[str] = None   # тейк 2 — 50% от остатка
    take3_order_id: Optional[str] = None   # тейк 3 — весь остаток
    take1_price:    float = 0.0            # цена тейка 1
    take2_price:    float = 0.0            # цена тейка 2
    filled:  bool = False
    taken:   bool = False
    taken2:  bool = False
    taken3:  bool = False


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
    # ШАГ 1а: /stop — стоп-маркет на вход (заблаговременно)
    # buy:  активируется когда цена вырастет до PRICE
    # sell: активируется когда цена упадёт до PRICE
    # ─────────────────────────────────────────────────────────────────────────
    async def place_stop_entry(self, symbol: str, side: str,
                               usdt_amount: float, price: float,
                               sl_price: float, trim_pct: float,
                               leverage: int) -> str:

        old = self._plans.get(symbol)
        if old:
            for oid in [old.sl_order_id, old.take_order_id, old.take2_order_id]:
                if oid:
                    try:
                        await self.client.cancel_order(oid)
                    except Exception as e:
                        logger.warning(f"Could not cancel old order {oid}: {e}")

        contracts, _, multiplier = await self.client.usdt_to_contracts(symbol, usdt_amount, price)
        actual_usdt = contracts * price * multiplier
        stop_direction = "up" if side == "buy" else "down"

        data     = await self.client.place_stop_market_entry(
            symbol, side, contracts, price, stop_direction, leverage
        )
        order_id = data.get("orderId", "")

        self._plans[symbol] = Plan(
            symbol=symbol, side=side,
            entry_price=price, contracts=contracts,
            sl_price=sl_price, trim_pct=trim_pct,
            leverage=leverage, entry_order_id=order_id,
        )

        sl_line = f"\n  🛑 Стоп-лосс: `{sl_price}`" if sl_price > 0 else ""
        logger.info(f"Stop entry: {symbol} {side} {contracts}c @{price} SL={sl_price} → {order_id}")
        await self._send(
            f"🎯 *Стоп-маркет на вход выставлен*\n"
            f"Символ: `{symbol}`\n"
            f"Направление: `{'LONG 📈' if side=='buy' else 'SHORT 📉'}`\n"
            f"Размер: `{actual_usdt:.2f} USDT` → `{contracts}` контрактов\n"
            f"{'📈 Вход при росте до' if side=='buy' else '📉 Вход при падении до'} `{price}`\n"
            f"Плечо: `{leverage}x`\n"
            f"После исполнения:{sl_line}\n"
            f"  ✂️ Тейк 1: 50% при `+{trim_pct}%`\n"
            f"  ✂️ Тейк 2: 50% от остатка при ещё `+{trim_pct}%`\n"
            f"  🎯 Безубыток и перенос стопа автоматически\n"
            f"ID: `{order_id}`"
        )
        return order_id

    # ─────────────────────────────────────────────────────────────────────────
    # ШАГ 1б: /open — лимитный ордер на вход
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
    # ШАГ 1в: Маркет-ордер с планом (для алертов)
    # Вход по рынку + полная автологика тейков/SL/безубытка
    # ─────────────────────────────────────────────────────────────────────────
    async def place_market_with_plan(self, symbol: str, side: str,
                                     usdt_amount: float, sl_price: float,
                                     trim_pct: float, leverage: int) -> str:
        """
        Рыночный ордер + создание плана для автоматических тейков/SL.
        Вызывается из AlertManager при срабатывании ценового алерта.
        Та же логика что и place_limit_order / place_stop_entry,
        но вход по рынку — on_order_filled подхватит и выставит тейки.
        """
        # Отменяем старый план если есть
        old = self._plans.get(symbol)
        if old:
            for oid in [old.sl_order_id, old.take_order_id,
                        old.take2_order_id, old.take3_order_id]:
                if oid:
                    try:
                        await self.client.cancel_order(oid)
                    except Exception as e:
                        logger.warning(f"Could not cancel old order {oid}: {e}")

        contracts, price, multiplier = await self.client.usdt_to_contracts(
            symbol, usdt_amount
        )
        actual_usdt = contracts * price * multiplier

        data     = await self.client.place_market_order(
            symbol, side, contracts, leverage
        )
        order_id = data.get("orderId", "")

        # Создаём план — on_order_filled подхватит по entry_order_id
        self._plans[symbol] = Plan(
            symbol=symbol, side=side,
            entry_price=price,   # будет уточнена из позиции в on_order_filled
            contracts=contracts,
            sl_price=sl_price, trim_pct=trim_pct,
            leverage=leverage, entry_order_id=order_id,
        )

        sl_line = f"\n  🛑 Стоп-лосс: `{sl_price}`" if sl_price > 0 else ""
        logger.info(f"Market with plan: {symbol} {side} {contracts}c "
                    f"SL={sl_price} trim={trim_pct}% → {order_id}")
        await self._send(
            f"📋 *Маркет-ордер с планом (алерт)*\n"
            f"Символ: `{symbol}`\n"
            f"Направление: `{'LONG 📈' if side=='buy' else 'SHORT 📉'}`\n"
            f"Размер: `{actual_usdt:.2f} USDT` → `{contracts}` контрактов\n"
            f"Плечо: `{leverage}x`\n"
            f"После исполнения:{sl_line}\n"
            f"  ✂️ Тейк 1: 50% при `+{trim_pct}%`\n"
            f"  ✂️ Тейк 2: 50% от остатка при ещё `+{trim_pct}%`\n"
            f"  🎯 Безубыток и перенос стопа автоматически\n"
            f"ID: `{order_id}`"
        )
        return order_id

    # ─────────────────────────────────────────────────────────────────────────
    # ШАГ 2: после исполнения лимитки — выставляем тейк и SL
    # ─────────────────────────────────────────────────────────────────────────
    async def _on_entry_filled(self, plan: Plan, fill_price: float):
        plan.entry_price = fill_price
        close_side  = "sell" if plan.side == "buy" else "buy"

        logger.info(f"_on_entry_filled: symbol={plan.symbol} side={plan.side} "
                    f"fill={fill_price} sl={plan.sl_price} trim={plan.trim_pct}% "
                    f"contracts={plan.contracts}")

        # При 1 контракте порез невозможен — ставим только SL, тейк на весь объём
        if plan.contracts <= 1:
            half = 1
            await self._send(
                f"⚠️ *Позиция слишком мала для пореза*\n"
                f"Символ: `{plan.symbol}` — всего `{plan.contracts}` контракт(ов)\n"
                f"Тейк будет на весь объём (без пореза 50%)"
            )
        else:
            half = plan.contracts // 2

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
    # ШАГ 3: тейк 1 исполнился — безубыток + тейк 2
    # ─────────────────────────────────────────────────────────────────────────
    async def _on_take_filled(self, plan: Plan, fill_price: float):
        plan.take1_price = fill_price   # запоминаем цену тейка 1
        close_side      = "sell" if plan.side == "buy" else "buy"
        half            = plan.contracts // 2
        remaining       = max(1, plan.contracts - half)  # ~50% остаток
        quarter         = max(1, remaining // 2)          # 50% от остатка

        if plan.side == "buy":
            be_direction   = "down"
            take2_price    = round(fill_price * (1 + plan.trim_pct / 100), 8)
            take2_direction = "up"
        else:
            be_direction   = "up"
            take2_price    = round(fill_price * (1 - plan.trim_pct / 100), 8)
            take2_direction = "down"

        # Отменяем старый SL
        if plan.sl_order_id:
            try:
                await self.client.cancel_order(plan.sl_order_id)
                logger.info(f"Cancelled old SL {plan.sl_order_id}")
            except Exception as e:
                logger.warning(f"Cancel SL failed: {e}")
            plan.sl_order_id = None

        # Безубыток на цену входа для остатка
        try:
            data = await self.client.place_stop_market_close(
                plan.symbol, close_side, remaining,
                plan.entry_price, be_direction, plan.leverage
            )
            plan.sl_order_id = data.get("orderId", "")
            logger.info(f"Breakeven: {plan.symbol} @ {plan.entry_price} → {plan.sl_order_id}")
            await self._send(
                f"🎯 *Стоп в безубыток*\n"
                f"Символ: `{plan.symbol}`\n"
                f"Тейк 1 исполнен по: `{fill_price}`\n"
                f"Стоп на цене входа: `{plan.entry_price}`\n"
                f"Остаток: `{remaining}` контрактов\n"
                f"ID: `{plan.sl_order_id}`"
            )
        except Exception as e:
            logger.error(f"Breakeven failed: {e}")
            await self._send(f"⚠️ Безубыток не выставлен: `{e}`\nСтоп вручную на: `{plan.entry_price}`")

        # Тейк 2 — 50% от остатка при движении ещё на trim_pct%
        try:
            data = await self.client.place_stop_market_close(
                plan.symbol, close_side, quarter,
                take2_price, take2_direction, plan.leverage
            )
            plan.take2_order_id = data.get("orderId", "")
            logger.info(f"Take2: {plan.symbol} {quarter}c @ {take2_price} → {plan.take2_order_id}")
            await self._send(
                f"✂️ *Тейк 2 выставлен*\n"
                f"Символ: `{plan.symbol}`\n"
                f"Цена тейка 2: `{take2_price}` (`+{plan.trim_pct}%` от тейка 1)\n"
                f"Контрактов: `{quarter}` из `{remaining}`\n"
                f"ID: `{plan.take2_order_id}`"
            )
        except Exception as e:
            logger.error(f"Take2 failed: {e}")
            await self._send(f"⚠️ Тейк 2 не выставлен: `{e}`")

    # ─────────────────────────────────────────────────────────────────────────
    # ШАГ 4: тейк 2 исполнился — стоп на тейк 1 + тейк 3 (весь остаток)
    # ─────────────────────────────────────────────────────────────────────────
    async def _on_take2_filled(self, plan: Plan, fill_price: float):
        plan.take2_price = fill_price
        close_side       = "sell" if plan.side == "buy" else "buy"

        # Считаем остаток: исходные - тейк1(50%) - тейк2(25%) = 25%
        half            = plan.contracts // 2
        remaining_after1 = max(1, plan.contracts - half)
        quarter         = max(1, remaining_after1 // 2)
        final_remaining = max(1, remaining_after1 - quarter)

        take1 = plan.take1_price

        if plan.side == "buy":
            direction      = "down"
            take3_price    = round(fill_price * (1 + plan.trim_pct / 100), 8)
            take3_direction = "up"
        else:
            direction      = "up"
            take3_price    = round(fill_price * (1 - plan.trim_pct / 100), 8)
            take3_direction = "down"

        # Отменяем текущий стоп (безубыток)
        if plan.sl_order_id:
            try:
                await self.client.cancel_order(plan.sl_order_id)
                logger.info(f"Cancelled SL {plan.sl_order_id}")
            except Exception as e:
                logger.warning(f"Cancel SL failed: {e}")
            plan.sl_order_id = None

        # Стоп на цену тейка 1
        try:
            data = await self.client.place_stop_market_close(
                plan.symbol, close_side, final_remaining,
                take1, direction, plan.leverage
            )
            plan.sl_order_id = data.get("orderId", "")
            logger.info(f"Stop at take1: {plan.symbol} @ {take1} → {plan.sl_order_id}")
            await self._send(
                f"🎯 *Стоп перенесён на тейк 1*\n"
                f"Символ: `{plan.symbol}`\n"
                f"Тейк 2 исполнен по: `{fill_price}`\n"
                f"Стоп на: `{take1}`\n"
                f"Остаток: `{final_remaining}` контрактов\n"
                f"ID: `{plan.sl_order_id}`"
            )
        except Exception as e:
            logger.error(f"Stop at take1 failed: {e}")
            await self._send(f"⚠️ Стоп не перенесён: `{e}`\nСтоп вручную на: `{take1}`")

        # Тейк 3 — весь остаток при движении ещё на trim_pct%
        try:
            data = await self.client.place_stop_market_close(
                plan.symbol, close_side, final_remaining,
                take3_price, take3_direction, plan.leverage
            )
            plan.take3_order_id = data.get("orderId", "")
            logger.info(f"Take3: {plan.symbol} {final_remaining}c @ {take3_price} → {plan.take3_order_id}")
            await self._send(
                f"✂️ *Тейк 3 выставлен (весь остаток)*\n"
                f"Символ: `{plan.symbol}`\n"
                f"Цена тейка 3: `{take3_price}` (`+{plan.trim_pct}%` от тейка 2)\n"
                f"Контрактов: `{final_remaining}`\n"
                f"ID: `{plan.take3_order_id}`"
            )
        except Exception as e:
            logger.error(f"Take3 failed: {e}")
            await self._send(f"⚠️ Тейк 3 не выставлен: `{e}`")

    # ─────────────────────────────────────────────────────────────────────────
    # ШАГ 5: тейк 3 исполнился — позиция закрыта полностью
    # ─────────────────────────────────────────────────────────────────────────
    async def _on_take3_filled(self, plan: Plan, fill_price: float):
        close_side = "sell" if plan.side == "buy" else "buy"

        # Отменяем стоп если остался
        if plan.sl_order_id:
            try:
                await self.client.cancel_order(plan.sl_order_id)
            except Exception:
                pass  # ордер уже исполнился или не существует — ок
            plan.sl_order_id = None

        await self._send(
            f"🏁 *Позиция закрыта полностью*\n"
            f"Символ: `{plan.symbol}`\n"
            f"Тейк 3 исполнен по: `{fill_price}`\n"
            f"Все 3 тейка отработали ✅"
        )

        if plan.symbol in self._plans:
            del self._plans[plan.symbol]

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
        status   = data.get("status", "")

        # При match-событии цена может быть 0 — берём марк-цену как fallback
        if price <= 0:
            try:
                price = await self.client.get_mark_price(symbol)
            except Exception:
                pass

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

        logger.info(f"Checking: event_id={order_id} status={status} price={price} "
                    f"entry={plan.entry_order_id} take={plan.take_order_id} "
                    f"take2={plan.take2_order_id} take3={plan.take3_order_id} "
                    f"filled={plan.filled} taken={plan.taken} taken2={plan.taken2} taken3={plan.taken3}")

        # Сравниваем orderId — WS может слать укороченный вариант
        def ids_match(a: str, b: str) -> bool:
            if not a or not b:
                return False
            return a == b or a.startswith(b) or b.startswith(a)

        # Лимитный/стоп-маркет/маркет вход исполнился → тейки + SL
        if ids_match(plan.entry_order_id, order_id) and not plan.filled:
            plan.filled = True
            # Берём реальную цену входа из позиции (точнее чем WS событие)
            try:
                pos = await self.client.get_position(symbol)
                if pos:
                    real_price = float(pos.get("avgEntryPrice", 0) or 0)
                    if real_price > 0:
                        logger.info(f"Entry price from position: {real_price} (WS gave: {price})")
                        price = real_price
            except Exception as e:
                logger.warning(f"Could not fetch position price: {e}")
            logger.info(f"Entry filled: {symbol} @ {price}")
            await self._on_entry_filled(plan, price)

        # Тейк 1 — только при финальном done чтобы не задвоить
        elif ids_match(plan.take_order_id, order_id) and not plan.taken:
            plan.taken = True
            logger.info(f"Take1 filled: {symbol} @ {price}")
            await self._on_take_filled(plan, price)

        # Тейк 2
        elif ids_match(plan.take2_order_id, order_id) and not plan.taken2:
            plan.taken2 = True
            logger.info(f"Take2 filled: {symbol} @ {price}")
            await self._on_take2_filled(plan, price)

        # Тейк 3
        elif ids_match(plan.take3_order_id, order_id) and not plan.taken3:
            plan.taken3 = True
            logger.info(f"Take3 filled: {symbol} @ {price}")
            await self._on_take3_filled(plan, price)

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
