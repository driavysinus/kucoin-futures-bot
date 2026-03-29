"""
alert_manager.py

Система ценовых алертов:
  1. Пользователь добавляет алерт через Telegram или консоль:
       /alert SYMBOL PRICE SIDE USDT SL TRIG% LEV
       add SYMBOL PRICE SIDE USDT SL TRIG% LEV
  2. FuturesMonitor шлёт price_update → AlertManager проверяет
  3. При достижении цены → маркет-ордер + полная логика тейков/SL/безубытка
  4. Алерты сохраняются в файл — переживают перезапуск бота
"""

import asyncio
import json
import os
from dataclasses import dataclass, field, asdict
from typing import Optional, Callable
from loguru import logger

ALERTS_FILE = "alerts.json"


@dataclass
class Alert:
    id:          int
    symbol:      str          # XBTUSDTM
    trigger_price: float      # цена срабатывания
    side:        str          # buy / sell
    usdt_amount: float        # размер в USDT
    sl_price:    float        # стоп-лосс (0 = без стопа)
    trim_pct:    float        # % для тейков
    leverage:    int
    fired:       bool = False

    # Для определения направления пересечения:
    #   buy  (long)  — цена упала ДО trigger_price или ниже → вход
    #   sell (short) — цена выросла ДО trigger_price или выше → вход
    #
    # Но пользователь может хотеть и обратную логику (вход на пробое):
    #   buy  на пробое вверх — цена >= trigger
    #   sell на пробое вниз  — цена <= trigger
    #
    # Используем простую логику:
    #   buy:  срабатывает когда цена <= trigger_price  (покупаем на уровне/ниже)
    #   sell: срабатывает когда цена >= trigger_price  (продаём на уровне/выше)
    #
    # Для пробойных входов (buy при росте) — используйте /stop команду.
    direction: str = ""       # "down" для buy, "up" для sell — задаётся автоматически

    def __post_init__(self):
        if not self.direction:
            self.direction = "down" if self.side == "buy" else "up"


class AlertManager:
    def __init__(self, order_manager, monitor, notify: Callable = None):
        """
        order_manager: OrderManager — для размещения ордеров
        monitor:       FuturesMonitor — для подписки на тикеры
        notify:        async callable для отправки сообщений в Telegram
        """
        self.order_manager = order_manager
        self.monitor       = monitor
        self._notify       = notify or (lambda msg: None)
        self._alerts: dict[int, Alert] = {}
        self._next_id      = 1
        self._processing: set[int] = set()         # защита от двойного срабатывания

        # Загружаем сохранённые алерты с диска
        self._load_alerts()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _save_alerts(self):
        """Сохранить активные алерты в JSON файл."""
        try:
            active = [asdict(a) for a in self._alerts.values() if not a.fired]
            data = {
                "next_id": self._next_id,
                "alerts":  active,
            }
            with open(ALERTS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.debug(f"Saved {len(active)} alerts to {ALERTS_FILE}")
        except Exception as e:
            logger.error(f"Failed to save alerts: {e}")

    def _load_alerts(self):
        """Загрузить алерты из JSON файла при старте."""
        if not os.path.exists(ALERTS_FILE):
            return

        try:
            with open(ALERTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)

            self._next_id = data.get("next_id", 1)
            loaded = 0
            for item in data.get("alerts", []):
                alert = Alert(
                    id=item["id"],
                    symbol=item["symbol"],
                    trigger_price=item["trigger_price"],
                    side=item["side"],
                    usdt_amount=item["usdt_amount"],
                    sl_price=item["sl_price"],
                    trim_pct=item["trim_pct"],
                    leverage=item["leverage"],
                    fired=False,
                    direction=item.get("direction", ""),
                )
                self._alerts[alert.id] = alert
                # Подписаться на тикер
                self.monitor.subscribe_ticker(alert.symbol)
                loaded += 1

            if loaded:
                logger.info(f"Loaded {loaded} alerts from {ALERTS_FILE}")
        except Exception as e:
            logger.error(f"Failed to load alerts from {ALERTS_FILE}: {e}")

    async def _send(self, msg: str):
        try:
            if asyncio.iscoroutinefunction(self._notify):
                await self._notify(msg)
            else:
                self._notify(msg)
        except Exception as e:
            logger.error(f"Alert notify error: {e}")

    # ── Управление алертами ──────────────────────────────────────────────────

    def add_alert(self, symbol: str, trigger_price: float, side: str,
                  usdt_amount: float, sl_price: float = 0.0,
                  trim_pct: float = None, leverage: int = None) -> Alert:
        """Добавить ценовой алерт. Возвращает созданный Alert."""
        from config import DEFAULT_PROFIT_TRIGGER_PCT, DEFAULT_LEVERAGE

        alert = Alert(
            id=self._next_id,
            symbol=symbol,
            trigger_price=trigger_price,
            side=side,
            usdt_amount=usdt_amount,
            sl_price=sl_price,
            trim_pct=trim_pct if trim_pct is not None else DEFAULT_PROFIT_TRIGGER_PCT,
            leverage=leverage if leverage is not None else DEFAULT_LEVERAGE,
        )
        self._alerts[self._next_id] = alert
        self._next_id += 1

        # Подписаться на тикер этого символа
        self.monitor.subscribe_ticker(symbol)

        logger.info(f"Alert #{alert.id} added: {symbol} {side} @ {trigger_price} "
                    f"USDT={usdt_amount} SL={sl_price} trim={alert.trim_pct}% lev={alert.leverage}x")
        self._save_alerts()
        return alert

    def remove_alert(self, alert_id: int) -> Optional[Alert]:
        """Удалить алерт по ID."""
        alert = self._alerts.pop(alert_id, None)
        if alert:
            logger.info(f"Alert #{alert_id} removed: {alert.symbol}")
            self._save_alerts()
        return alert

    def list_alerts(self) -> list[Alert]:
        """Список активных (не сработавших) алертов."""
        return [a for a in self._alerts.values() if not a.fired]

    def clear_alerts(self, symbol: str = None):
        """Удалить все алерты (или по символу)."""
        if symbol:
            to_del = [aid for aid, a in self._alerts.items() if a.symbol == symbol]
        else:
            to_del = list(self._alerts.keys())
        for aid in to_del:
            del self._alerts[aid]
        logger.info(f"Cleared {len(to_del)} alerts" + (f" for {symbol}" if symbol else ""))
        self._save_alerts()

    # ── Обработка цены ───────────────────────────────────────────────────────

    async def on_price_update(self, data: dict):
        """
        Вызывается из FuturesMonitor на каждый price_update.
        Проверяет все алерты для данного символа.

        Логика срабатывания (без ожидания пересечения):
          buy:  цена <= trigger_price  → вход в лонг
          sell: цена >= trigger_price  → вход в шорт
        Срабатывает сразу на первом же тике, если цена уже на уровне.
        """
        symbol = data.get("symbol", "")
        price  = data.get("price", 0)
        if price is None:
            return
        price = float(price)
        if price <= 0:
            return

        # Проверяем алерты для этого символа
        relevant = [a for a in self._alerts.values()
                    if not a.fired and a.symbol == symbol and a.id not in self._processing]

        for alert in relevant:
            triggered = False

            if alert.side == "buy" and price <= alert.trigger_price:
                triggered = True
            elif alert.side == "sell" and price >= alert.trigger_price:
                triggered = True

            if triggered:
                alert.fired = True
                self._processing.add(alert.id)
                # Запускаем в фоне чтобы не блокировать обработку цен
                asyncio.create_task(self._execute_alert(alert, price))

    async def _execute_alert(self, alert: Alert, current_price: float):
        """Исполнить алерт: маркет-ордер + полная логика тейков/SL."""
        try:
            logger.info(f"🔔 Alert #{alert.id} TRIGGERED: {alert.symbol} {alert.side} "
                        f"@ {current_price} (trigger was {alert.trigger_price})")

            await self._send(
                f"🔔 *Алерт #{alert.id} сработал!*\n"
                f"Символ: `{alert.symbol}`\n"
                f"Цена достигла: `{current_price}` "
                f"(уровень: `{alert.trigger_price}`)\n"
                f"Открываю `{'LONG 📈' if alert.side == 'buy' else 'SHORT 📉'}`…"
            )

            # Устанавливаем плечо
            self.order_manager.set_leverage(alert.symbol, alert.leverage)

            # Маркет-ордер + создание плана для автологики тейков/SL
            await self.order_manager.place_market_with_plan(
                symbol=alert.symbol,
                side=alert.side,
                usdt_amount=alert.usdt_amount,
                sl_price=alert.sl_price,
                trim_pct=alert.trim_pct,
                leverage=alert.leverage,
            )

        except Exception as e:
            logger.error(f"Alert #{alert.id} execution failed: {e}", exc_info=True)
            await self._send(
                f"❌ *Ошибка исполнения алерта #{alert.id}*\n"
                f"Символ: `{alert.symbol}`\n"
                f"Ошибка: `{e}`"
            )
        finally:
            self._processing.discard(alert.id)
            # Удаляем сработавший алерт
            self._alerts.pop(alert.id, None)
            self._save_alerts()
