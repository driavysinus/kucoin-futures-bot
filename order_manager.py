"""
order_manager.py — Парадигма Фен-Шуй (WebSocket only)

SL и TP НЕ выставляются на бирже. Мониторинг через WebSocket price_update.
При касании уровня — маркет-ордер на закрытие. Биржа не знает про SL/TP.

Логика:
  1 stop пройден → порез 50% от начального, SL/TP не меняются
  2 стопа       → порез 50% от остатка, SL на entry+1×stop (старт трейлинга), TP без изменений
  3+ стопов     → порезов нет; TP углубляется на +1×stop за каждый новый уровень;
                   SL динамически трейлит за ценой на расстоянии 1×stop (только в плюс)
"""

import asyncio
import json
import math
import os
from dataclasses import asdict, dataclass
from typing import Optional, Callable
from loguru import logger

from kucoin_client import KuCoinFuturesClient
import config


PLANS_FILE = "plans.json"


@dataclass
class Plan:
    symbol:         str
    side:           str        # buy / sell
    entry_price:    float      # цена входа (уточняется из позиции)
    contracts:      int        # начальный объём
    initial_sl:     float      # начальный SL (не меняется, для расчёта stop_size)
    stop_size:      float      # abs(entry - initial_sl) — НЕ МЕНЯЕТСЯ
    leverage:       int

    # Текущее состояние
    remaining:      int = 0    # контрактов в рынке
    stops_passed:   int = 0    # сколько stop_size пройдено
    current_sl:     float = 0  # текущий SL (виртуальный, не на бирже)
    current_tp:     float = 0  # текущий TP (виртуальный, не на бирже)

    # ID ордеров
    entry_order_id: Optional[str] = None

    filled: bool = False       # вход исполнен
    sl_triggered: bool = False # SL уже сработал (защита от повтора)
    tp_triggered: bool = False # TP уже сработал (защита от повтора)

    # Для обратной совместимости
    sl_price:    float = 0
    trim_pct:    float = 0


class OrderManager:
    def __init__(self, client: KuCoinFuturesClient, notify: Callable = None):
        self.client   = client
        self._notify  = notify or (lambda msg: None)
        self._plans:    dict[str, Plan] = {}
        self._leverage: dict[str, int] = {}
        self._load_plans()

    def set_leverage(self, symbol: str, lev: int):
        self._leverage[symbol] = lev

    def get_leverage(self, symbol: str) -> int:
        return self._leverage.get(symbol, config.DEFAULT_LEVERAGE)

    def get_plan_symbols(self) -> list[str]:
        return list(self._plans.keys())

    # ─── Persistence ─────────────────────────────────────────────────────────

    def _save_plans(self):
        """Сохранить активные планы сопровождения на диск."""
        try:
            plans = []
            for plan in self._plans.values():
                if plan.filled and plan.remaining <= 0:
                    continue
                item = asdict(plan)
                # Эти флаги защищают только текущий процесс от дублей.
                # После рестарта открытая позиция должна снова проверяться.
                item["sl_triggered"] = False
                item["tp_triggered"] = False
                plans.append(item)

            data = {
                "version": 1,
                "plans": plans,
            }
            tmp_file = f"{PLANS_FILE}.tmp"
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp_file, PLANS_FILE)
            logger.debug(f"Saved {len(plans)} plans to {PLANS_FILE}")
        except Exception as e:
            logger.error(f"Failed to save plans to {PLANS_FILE}: {e}")

    def _load_plans(self):
        """Загрузить планы сопровождения после рестарта."""
        if not os.path.exists(PLANS_FILE):
            return

        try:
            with open(PLANS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            items = data.get("plans", []) if isinstance(data, dict) else data
            loaded = 0
            for item in items:
                try:
                    symbol = str(item["symbol"]).upper()
                    side = str(item["side"]).lower()
                    contracts = int(float(item["contracts"]))
                    initial_sl = float(item["initial_sl"])
                    entry_price = float(item["entry_price"])
                    stop_size = float(item.get("stop_size", abs(entry_price - initial_sl)))
                    remaining = int(float(item.get("remaining", contracts)))

                    plan = Plan(
                        symbol=symbol,
                        side=side,
                        entry_price=entry_price,
                        contracts=contracts,
                        initial_sl=initial_sl,
                        stop_size=stop_size,
                        leverage=int(float(item.get("leverage", config.DEFAULT_LEVERAGE))),
                        remaining=remaining,
                        stops_passed=int(float(item.get("stops_passed", 0))),
                        current_sl=float(item.get("current_sl", initial_sl)),
                        current_tp=float(item.get("current_tp", 0)),
                        entry_order_id=item.get("entry_order_id"),
                        filled=bool(item.get("filled", False)),
                        sl_triggered=False,
                        tp_triggered=False,
                        sl_price=float(item.get("sl_price", initial_sl)),
                        trim_pct=float(item.get("trim_pct", 0)),
                    )
                    if plan.filled and plan.remaining <= 0:
                        continue
                    if plan.current_tp <= 0 and plan.stop_size > 0:
                        if plan.side == "buy":
                            plan.current_tp = round(plan.entry_price + 3 * plan.stop_size, 8)
                        else:
                            plan.current_tp = round(plan.entry_price - 3 * plan.stop_size, 8)
                    self._plans[symbol] = plan
                    self._leverage[symbol] = plan.leverage
                    loaded += 1
                except Exception as e:
                    logger.error(f"Skipping broken plan in {PLANS_FILE}: {e}")

            if loaded:
                logger.info(f"Loaded {loaded} plans from {PLANS_FILE}")
        except Exception as e:
            logger.error(f"Failed to load plans from {PLANS_FILE}: {e}")

    def _remove_plan(self, symbol: str, save: bool = True) -> Optional[Plan]:
        plan = self._plans.pop(symbol, None)
        if plan and save:
            self._save_plans()
        return plan

    async def reconcile_restored_plans(self):
        """
        Сверить восстановленные планы с биржей.
        Если бот падал после исполнения входа, но до обработки WS-fill,
        план переводится в filled по реальной позиции.
        """
        if not self._plans:
            return

        changed = False
        restored = 0

        for symbol, plan in list(self._plans.items()):
            try:
                position = await self.client.get_position(symbol)
            except Exception as e:
                logger.warning(f"Could not reconcile restored plan {symbol}: {e}")
                continue

            qty = float(position.get("currentQty", 0)) if position else 0.0
            real_qty = int(abs(qty))

            if plan.filled:
                if real_qty <= 0:
                    logger.info(f"Restored plan {symbol} has no open position; removing")
                    self._remove_plan(symbol, save=False)
                    changed = True
                    continue

                real_side = "buy" if qty > 0 else "sell"
                if real_side != plan.side:
                    logger.warning(
                        f"Restored plan {symbol} side mismatch: "
                        f"plan={plan.side}, exchange={real_side}; removing plan"
                    )
                    await self._send(
                        f"⚠️ *План `{symbol}` не восстановлен*\n"
                        f"Сторона плана: `{plan.side}`, на бирже: `{real_side}`.\n"
                        f"Сопровождение снято, чтобы не управлять чужой позицией."
                    )
                    self._remove_plan(symbol, save=False)
                    changed = True
                    continue

                if plan.remaining != real_qty:
                    logger.info(f"Restore sync {symbol}: remaining {plan.remaining} -> {real_qty}")
                    plan.remaining = real_qty
                    changed = True
                plan.sl_triggered = False
                plan.tp_triggered = False
                self._leverage[symbol] = plan.leverage
                restored += 1
                continue

            if real_qty <= 0:
                self._leverage[symbol] = plan.leverage
                continue

            real_side = "buy" if qty > 0 else "sell"
            if real_side != plan.side:
                logger.warning(
                    f"Pending restored plan {symbol} side mismatch: "
                    f"plan={plan.side}, exchange={real_side}; keeping pending plan"
                )
                continue

            fill_price = float(position.get("avgEntryPrice", 0) or 0)
            if fill_price <= 0:
                fill_price = plan.entry_price

            plan.filled = True
            plan.entry_price = fill_price
            plan.stop_size = abs(fill_price - plan.initial_sl)
            plan.remaining = real_qty
            plan.current_sl = plan.initial_sl
            if plan.side == "buy":
                plan.current_tp = round(fill_price + 3 * plan.stop_size, 8)
            else:
                plan.current_tp = round(fill_price - 3 * plan.stop_size, 8)
            plan.sl_triggered = False
            plan.tp_triggered = False
            self._leverage[symbol] = plan.leverage
            changed = True
            restored += 1

            await self._send(
                f"♻️ *План `{symbol}` восстановлен после рестарта*\n"
                f"Цена входа: `{plan.entry_price}`\n"
                f"Контрактов: `{plan.remaining}`\n"
                f"SL: `{plan.current_sl}` | TP: `{plan.current_tp}`\n"
                f"Stop size: `{plan.stop_size}`"
            )

        if changed:
            self._save_plans()
        if restored:
            logger.info(f"Reconciled {restored} restored plans")

    async def _send(self, msg: str):
        try:
            if asyncio.iscoroutinefunction(self._notify):
                await self._notify(msg)
            else:
                self._notify(msg)
        except Exception as e:
            logger.error(f"Notify error: {e}")

    # ─── Helpers ──────────────────────────────────────────────────────────────

    def _close_side(self, plan: Plan) -> str:
        return "sell" if plan.side == "buy" else "buy"

    def _price_at_stops(self, plan: Plan, n_stops: int) -> float:
        if plan.side == "buy":
            return round(plan.entry_price + n_stops * plan.stop_size, 8)
        else:
            return round(plan.entry_price - n_stops * plan.stop_size, 8)

    async def _close_position_market(self, plan: Plan, size: int, reason: str) -> Optional[str]:
        """Закрыть часть позиции маркет-ордером (reduceOnly)."""
        if size <= 0:
            return None
        try:
            data = await self.client.place_market_close(
                plan.symbol, self._close_side(plan), size, plan.leverage
            )
            oid = data.get("orderId", "")
            logger.info(f"{reason}: {plan.symbol} closed {size}c -> {oid}")
            return oid
        except Exception as e:
            logger.error(f"{reason} failed: {e}")
            await self._send(f"⚠️ {reason} не удался для `{plan.symbol}`: `{e}`")
            return None

    # ═════════════════════════════════════════════════════════════════════════
    #  ВХОД В ПОЗИЦИЮ
    # ═════════════════════════════════════════════════════════════════════════

    def _create_plan(self, symbol: str, side: str, contracts: int,
                     entry_price: float, sl_price: float,
                     leverage: int, entry_order_id: str) -> Plan:
        stop_size = abs(entry_price - sl_price)

        if side == "buy":
            tp_price = round(entry_price + 3 * stop_size, 8)
        else:
            tp_price = round(entry_price - 3 * stop_size, 8)

        plan = Plan(
            symbol=symbol, side=side,
            entry_price=entry_price, contracts=contracts,
            initial_sl=sl_price, stop_size=stop_size,
            leverage=leverage,
            remaining=contracts, stops_passed=0,
            current_sl=sl_price, current_tp=tp_price,
            entry_order_id=entry_order_id,
            sl_price=sl_price,
        )
        self._plans[symbol] = plan
        self._save_plans()
        return plan

    # ── /stop — стоп-маркет на вход ──────────────────────────────────────────
    async def place_stop_entry(self, symbol: str, side: str,
                               usdt_amount: float, price: float,
                               sl_price: float, trim_pct: float,
                               leverage: int) -> str:

        old = self._plans.get(symbol)
        if old:
            self._remove_plan(symbol)

        contracts, _, multiplier = await self.client.usdt_to_contracts(symbol, usdt_amount, price)
        actual_usdt = contracts * price * multiplier
        stop_direction = "up" if side == "buy" else "down"

        data = await self.client.place_stop_market_entry(
            symbol, side, contracts, price, stop_direction, leverage
        )
        order_id = data.get("orderId", "")

        plan = self._create_plan(symbol, side, contracts, price, sl_price, leverage, order_id)

        logger.info(f"Stop entry: {symbol} {side} {contracts}c @{price} "
                    f"SL={sl_price} stop_size={plan.stop_size} -> {order_id}")
        await self._send(
            f"🎯 *Стоп-маркет на вход*\n"
            f"Символ: `{symbol}` | `{'LONG 📈' if side=='buy' else 'SHORT 📉'}`\n"
            f"Размер: `{actual_usdt:.2f} USDT` -> `{contracts}` контрактов\n"
            f"Вход: `{price}` | Плечо: `{leverage}x`\n"
            f"SL: `{sl_price}` | TP: `{plan.current_tp}` (3x stop)\n"
            f"Stop size: `{plan.stop_size}`\n"
            f"_SL/TP мониторятся через WS, не на бирже_\n"
            f"ID: `{order_id}`"
        )
        return order_id

    # ── /open — лимитный ордер на вход ───────────────────────────────────────
    async def place_limit_order(self, symbol: str, side: str,
                                usdt_amount: float, price: float,
                                sl_price: float, trim_pct: float,
                                leverage: int) -> str:

        old = self._plans.get(symbol)
        if old:
            self._remove_plan(symbol)

        contracts, _, multiplier = await self.client.usdt_to_contracts(symbol, usdt_amount, price)
        actual_usdt = contracts * price * multiplier

        data = await self.client.place_limit_order(symbol, side, contracts, price, leverage)
        order_id = data.get("orderId", "")

        plan = self._create_plan(symbol, side, contracts, price, sl_price, leverage, order_id)

        logger.info(f"Limit order: {symbol} {side} {contracts}c @{price} "
                    f"SL={sl_price} stop_size={plan.stop_size} -> {order_id}")
        await self._send(
            f"📋 *Лимитный ордер*\n"
            f"Символ: `{symbol}` | `{'LONG 📈' if side=='buy' else 'SHORT 📉'}`\n"
            f"Размер: `{actual_usdt:.2f} USDT` -> `{contracts}` контрактов\n"
            f"Цена: `{price}` | Плечо: `{leverage}x`\n"
            f"SL: `{sl_price}` | TP: `{plan.current_tp}` (3x stop)\n"
            f"Stop size: `{plan.stop_size}`\n"
            f"_SL/TP мониторятся через WS, не на бирже_\n"
            f"ID: `{order_id}`"
        )
        return order_id

    # ── Маркет с планом (для алертов) ────────────────────────────────────────
    async def place_market_with_plan(self, symbol: str, side: str,
                                     usdt_amount: float, sl_price: float,
                                     trim_pct: float, leverage: int) -> str:

        old = self._plans.get(symbol)
        if old:
            self._remove_plan(symbol)

        contracts, price, multiplier = await self.client.usdt_to_contracts(symbol, usdt_amount)
        actual_usdt = contracts * price * multiplier

        data = await self.client.place_market_order(symbol, side, contracts, leverage)
        order_id = data.get("orderId", "")

        plan = self._create_plan(symbol, side, contracts, price, sl_price, leverage, order_id)

        logger.info(f"Market with plan: {symbol} {side} {contracts}c "
                    f"SL={sl_price} stop_size={plan.stop_size} -> {order_id}")
        await self._send(
            f"📋 *Маркет-ордер (алерт)*\n"
            f"Символ: `{symbol}` | `{'LONG 📈' if side=='buy' else 'SHORT 📉'}`\n"
            f"Размер: `{actual_usdt:.2f} USDT` -> `{contracts}` контрактов\n"
            f"Плечо: `{leverage}x`\n"
            f"SL: `{sl_price}` | TP: `{plan.current_tp}` (3x stop)\n"
            f"Stop size: `{plan.stop_size}`\n"
            f"_SL/TP мониторятся через WS, не на бирже_\n"
            f"ID: `{order_id}`"
        )
        return order_id

    # ═════════════════════════════════════════════════════════════════════════
    #  ПОСЛЕ ИСПОЛНЕНИЯ ВХОДА
    # ═════════════════════════════════════════════════════════════════════════

    async def _on_entry_filled(self, plan: Plan, fill_price: float):
        plan.entry_price = fill_price
        plan.stop_size = abs(fill_price - plan.initial_sl)
        plan.remaining = plan.contracts

        if plan.side == "buy":
            plan.current_tp = round(fill_price + 3 * plan.stop_size, 8)
        else:
            plan.current_tp = round(fill_price - 3 * plan.stop_size, 8)

        plan.current_sl = plan.initial_sl
        self._save_plans()

        logger.info(f"Entry filled: {plan.symbol} {plan.side} @ {fill_price} "
                    f"SL={plan.current_sl} TP={plan.current_tp} "
                    f"stop_size={plan.stop_size} contracts={plan.contracts}")

        await self._send(
            f"✅ *Вход исполнен — Фен-Шуй (WS)*\n"
            f"Символ: `{plan.symbol}` | `{'LONG 📈' if plan.side=='buy' else 'SHORT 📉'}`\n"
            f"Цена входа: `{fill_price}`\n"
            f"Контрактов: `{plan.contracts}`\n"
            f"SL: `{plan.current_sl}` | TP: `{plan.current_tp}`\n"
            f"Stop size: `{plan.stop_size}`\n"
            f"Уровни: 1S=`{self._price_at_stops(plan, 1)}` "
            f"2S=`{self._price_at_stops(plan, 2)}` "
            f"3S=`{self._price_at_stops(plan, 3)}`\n"
            f"_Ордера SL/TP не на бирже — мониторинг WS_"
        )

    # ═════════════════════════════════════════════════════════════════════════
    #  МОНИТОРИНГ ЦЕНЫ — Фен-Шуй + SL/TP через WebSocket
    # ═════════════════════════════════════════════════════════════════════════

    async def on_price_update(self, data: dict):
        symbol = data.get("symbol", "")
        price  = data.get("price", 0)
        if price is None:
            return
        price = float(price)
        if price <= 0:
            return

        plan = self._plans.get(symbol)
        if not plan or not plan.filled or plan.remaining <= 0:
            return
        if plan.sl_triggered or plan.tp_triggered:
            return
        if plan.stop_size <= 0:
            return

        # ── Обработка уровней Феншуй (порезы + углубление TP) ────────────
        # Уровни обрабатываем ДО проверки SL/TP: при пересечении нового стопа
        # в trailing-режиме TP должен успеть углубиться, иначе сработает до сдвига.
        if plan.side == "buy":
            distance = price - plan.entry_price
        else:
            distance = plan.entry_price - price

        if distance > 0:
            current_stops = int(distance / plan.stop_size)
            if current_stops > plan.stops_passed:
                for level in range(plan.stops_passed + 1, current_stops + 1):
                    if plan.sl_triggered or plan.tp_triggered:
                        return
                    await self._handle_stop_level(plan, level, price)

        # ── Динамический трейлинг SL (активен после 2-го пореза) ─────────
        if plan.stops_passed >= 2:
            if self._update_trailing_sl(plan, price):
                self._save_plans()

        # ── Проверяем SL ──────────────────────────────────────────────────
        sl_hit = False
        if plan.side == "buy" and price <= plan.current_sl:
            sl_hit = True
        elif plan.side == "sell" and price >= plan.current_sl:
            sl_hit = True

        if sl_hit:
            plan.sl_triggered = True
            await self._execute_sl(plan, price)
            return

        # ── Проверяем TP ──────────────────────────────────────────────────
        tp_hit = False
        if plan.side == "buy" and price >= plan.current_tp:
            tp_hit = True
        elif plan.side == "sell" and price <= plan.current_tp:
            tp_hit = True

        if tp_hit:
            plan.tp_triggered = True
            await self._execute_tp(plan, price)
            return

    def _update_trailing_sl(self, plan: Plan, price: float) -> bool:
        """Динамический трейлинг: SL следует за ценой на 1×stop, только в плюс."""
        if plan.side == "buy":
            new_sl = round(price - plan.stop_size, 8)
            if new_sl > plan.current_sl:
                plan.current_sl = new_sl
                return True
        else:
            new_sl = round(price + plan.stop_size, 8)
            if new_sl < plan.current_sl:
                plan.current_sl = new_sl
                return True
        return False

    # ── Верификация закрытия позиции ────────────────────────────────────────
    async def _verify_position_closed(self, plan: Plan, reason: str) -> bool:
        """
        Проверяет, что позиция реально закрыта на бирже.
        Если нет — повторяет маркет-ордер до 3 раз.
        Возвращает True если позиция закрыта.
        """
        for attempt in range(3):
            await asyncio.sleep(0.5 + attempt * 0.5)
            try:
                position = await self.client.get_position(plan.symbol)
                if not position:
                    logger.info(f"{reason} verified: position {plan.symbol} closed")
                    return True
                real_qty = abs(float(position.get("currentQty", 0)))
                if real_qty == 0:
                    logger.info(f"{reason} verified: position {plan.symbol} qty=0")
                    return True

                # Позиция всё ещё открыта — повторяем
                logger.warning(f"{reason} attempt {attempt+1}: {plan.symbol} still has "
                               f"{int(real_qty)} contracts, retrying close...")
                await self._send(
                    f"⚠️ *Повторное закрытие (попытка {attempt+2})*\n"
                    f"Символ: `{plan.symbol}` | Осталось: `{int(real_qty)}` контрактов"
                )
                await self._close_position_market(plan, int(real_qty), f"{reason} retry")

            except Exception as e:
                logger.error(f"{reason} verify attempt {attempt+1} failed: {e}")

        # Финальная проверка
        await asyncio.sleep(1)
        try:
            position = await self.client.get_position(plan.symbol)
            if not position or abs(float(position.get("currentQty", 0))) == 0:
                return True
            remaining = abs(float(position.get("currentQty", 0)))
            logger.error(f"{reason}: FAILED to close {plan.symbol} after 3 retries, "
                         f"{int(remaining)} contracts remain!")
            await self._send(
                f"🚨 *КРИТИЧНО: позиция `{plan.symbol}` НЕ ЗАКРЫТА!*\n"
                f"Осталось: `{int(remaining)}` контрактов\n"
                f"Закройте вручную командой `/close {plan.symbol}`"
            )
            return False
        except Exception:
            return False

    # ── Исполнение SL через маркет ───────────────────────────────────────────
    async def _execute_sl(self, plan: Plan, price: float):
        logger.info(f"SL triggered (WS): {plan.symbol} price={price} sl={plan.current_sl}")

        oid = await self._close_position_market(plan, plan.remaining, "SL маркет")

        await self._send(
            f"🛑 *Стоп-лосс сработал (WS)*\n"
            f"Символ: `{plan.symbol}` | Цена: `{price}`\n"
            f"SL уровень: `{plan.current_sl}`\n"
            f"Закрыто: `{plan.remaining}` контрактов по рынку\n"
            f"ID: `{oid}`"
        )

        # Верификация: убеждаемся что позиция реально закрыта
        await self._verify_position_closed(plan, "SL")
        self._remove_plan(plan.symbol)

    # ── Исполнение TP через маркет ───────────────────────────────────────────
    async def _execute_tp(self, plan: Plan, price: float):
        logger.info(f"TP triggered (WS): {plan.symbol} price={price} tp={plan.current_tp}")

        oid = await self._close_position_market(plan, plan.remaining, "TP маркет")

        await self._send(
            f"🎯 *Тейк-профит сработал (WS)*\n"
            f"Символ: `{plan.symbol}` | Цена: `{price}`\n"
            f"TP уровень: `{plan.current_tp}`\n"
            f"Закрыто: `{plan.remaining}` контрактов по рынку\n"
            f"ID: `{oid}`"
        )

        # Верификация: убеждаемся что позиция реально закрыта
        await self._verify_position_closed(plan, "TP")
        self._remove_plan(plan.symbol)

    # ═════════════════════════════════════════════════════════════════════════
    #  УРОВНИ ФЕН-ШУЙ
    # ═════════════════════════════════════════════════════════════════════════

    async def _handle_stop_level(self, plan: Plan, level: int, current_price: float):
        # Проверяем реальную позицию
        try:
            position = await self.client.get_position(plan.symbol)
            if not position or float(position.get("currentQty", 0)) == 0:
                logger.warning(f"Feng Shui level {level}: position {plan.symbol} "
                               f"not found — removing plan")
                await self._send(
                    f"⚠️ *Позиция `{plan.symbol}` не найдена на бирже*\n"
                    f"Сопровождение остановлено."
                )
                self._remove_plan(plan.symbol)
                return

            real_qty = abs(float(position.get("currentQty", 0)))
            if int(real_qty) != plan.remaining:
                logger.info(f"Sync remaining: plan={plan.remaining} -> exchange={int(real_qty)}")
                plan.remaining = int(real_qty)
                self._save_plans()

            if plan.remaining <= 0:
                self._remove_plan(plan.symbol)
                return

        except Exception as e:
            logger.error(f"Position check failed: {e}")

        plan.stops_passed = level
        self._save_plans()
        logger.info(f"Feng Shui level {level}: {plan.symbol} price={current_price} "
                    f"remaining={plan.remaining}")

        if level == 1:
            await self._level_1_first_cut(plan, current_price)
        elif level == 2:
            await self._level_2_second_cut(plan, current_price)
        else:
            await self._level_n_trail(plan, level, current_price)

    # ── Уровень 1: Первый порез (SL не двигаем) ──────────────────────────────
    async def _level_1_first_cut(self, plan: Plan, current_price: float):
        cut = math.ceil(plan.contracts / 2)
        cut = min(cut, plan.remaining)

        if cut > 0:
            oid = await self._close_position_market(plan, cut, "Порез 1")
            if oid:
                plan.remaining -= cut
                self._save_plans()
            else:
                return

        # SL остаётся на initial — не меняем; TP тоже не трогаем

        await self._send(
            f"✂️ *Уровень 1 — Первый порез*\n"
            f"Символ: `{plan.symbol}` | Цена: `{current_price}`\n"
            f"Закрыто: `{cut}` контрактов (50% от `{plan.contracts}`)\n"
            f"Остаток: `{plan.remaining}`\n"
            f"SL: `{plan.current_sl}` (без изменений — initial)\n"
            f"TP: `{plan.current_tp}` (без изменений)"
        )

        if plan.remaining <= 0:
            await self._send(f"🏁 Позиция `{plan.symbol}` закрыта полностью")
            self._remove_plan(plan.symbol)

    # ── Уровень 2: Второй порез + старт трейлинга ────────────────────────────
    async def _level_2_second_cut(self, plan: Plan, current_price: float):
        cut = math.ceil(plan.remaining / 2)
        cut = min(cut, plan.remaining)

        if cut > 0:
            oid = await self._close_position_market(plan, cut, "Порез 2")
            if oid:
                plan.remaining -= cut
            else:
                return

        # SL двигаем на уровень первого пореза (entry ± 1×stop) —
        # это стартовая точка трейлинга, дальше он управляется динамически.
        if plan.side == "buy":
            plan.current_sl = round(plan.entry_price + plan.stop_size, 8)
        else:
            plan.current_sl = round(plan.entry_price - plan.stop_size, 8)
        # TP здесь не меняется — углубление начнётся с уровня 3+
        self._save_plans()

        await self._send(
            f"✂️ *Уровень 2 — Второй порез + старт трейлинга*\n"
            f"Символ: `{plan.symbol}` | Цена: `{current_price}`\n"
            f"Закрыто: `{cut}` контрактов (50% от остатка)\n"
            f"Остаток: `{plan.remaining}`\n"
            f"SL -> `{plan.current_sl}` (entry ± 1×stop, далее трейлинг)\n"
            f"TP: `{plan.current_tp}` (без изменений)\n"
            f"_Трейлинг-SL: цена ∓ 1×stop, динамически в плюс_"
        )

        if plan.remaining <= 0:
            await self._send(f"🏁 Позиция `{plan.symbol}` закрыта полностью")
            self._remove_plan(plan.symbol)

    # ── Уровень 3+: Углубление TP (SL трейлится в on_price_update) ───────────
    async def _level_n_trail(self, plan: Plan, level: int, current_price: float):
        if plan.side == "buy":
            plan.current_tp = round(plan.current_tp + plan.stop_size, 8)
        else:
            plan.current_tp = round(plan.current_tp - plan.stop_size, 8)
        self._save_plans()

        await self._send(
            f"📐 *Уровень {level} — Углубление TP*\n"
            f"Символ: `{plan.symbol}` | Цена: `{current_price}`\n"
            f"SL: `{plan.current_sl}` (трейлинг)\n"
            f"TP -> `{plan.current_tp}` (+1 stop)\n"
            f"Контрактов: `{plan.remaining}` | Порезов больше нет"
        )

    # ═════════════════════════════════════════════════════════════════════════
    #  РУЧНОЙ ПОРЕЗ
    # ═════════════════════════════════════════════════════════════════════════

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

        data     = await self.client.place_market_order(symbol, close_side, close_n, lev)
        order_id = data.get("orderId", "")

        plan = self._plans.get(symbol)
        if plan:
            plan.remaining = max(0, plan.remaining - close_n)
            if plan.remaining <= 0:
                self._remove_plan(symbol)
            else:
                self._save_plans()

        logger.info(f"Partial close: {symbol} {close_pct}% -> {close_n}/{int(current_qty)}c")
        usdt_str = f" (~`{close_usdt:.2f} USDT`)" if close_usdt else ""
        await self._send(
            f"✂️ *{reason}*\n"
            f"Символ: `{symbol}`\n"
            f"Закрыто: `{close_n}` из `{int(current_qty)}` контрактов (`{close_pct}%`){usdt_str}\n"
            f"ID: `{order_id}`"
        )
        return order_id

    # ═════════════════════════════════════════════════════════════════════════
    #  WebSocket — исполнение ордеров (только вход)
    # ═════════════════════════════════════════════════════════════════════════

    async def on_order_filled(self, data: dict):
        symbol   = data.get("symbol", "")
        side     = data.get("side", "")
        size     = data.get("filledSize", data.get("size", 0))
        price    = float(data.get("fillPrice", data.get("price", 0)) or 0)
        order_id = data.get("orderId", "")

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

        def ids_match(a: str, b: str) -> bool:
            if not a or not b:
                return False
            return a == b or a.startswith(b) or b.startswith(a)

        # Только вход — SL/TP теперь через on_price_update
        if ids_match(plan.entry_order_id, order_id) and not plan.filled:
            plan.filled = True
            try:
                pos = await self.client.get_position(symbol)
                if pos:
                    real_price = float(pos.get("avgEntryPrice", 0) or 0)
                    if real_price > 0:
                        logger.info(f"Entry price from position: {real_price} (WS: {price})")
                        price = real_price
            except Exception as e:
                logger.warning(f"Could not fetch position price: {e}")
            await self._on_entry_filled(plan, price)

    async def on_trailing_stop_triggered(self, data: dict):
        symbol = data.get("symbol", "")
        size   = data.get("size", "")
        await self._send(
            f"🛑 *Стоп сработал*\n"
            f"Символ: `{symbol}`\nЗакрыто: `{size}` контрактов"
        )
        self._remove_plan(symbol)

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

    # ═════════════════════════════════════════════════════════════════════════
    #  ОТМЕНА
    # ═════════════════════════════════════════════════════════════════════════

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
            self._remove_plan(symbol)
            await self._send(f"🗑 Все ордера по `{symbol}` отменены")
            return True
        except Exception as e:
            await self._send(f"⚠️ Ошибка отмены: {e}")
            return False
