"""
position_monitor.py
WebSocket-based real-time monitor:
  • Private channel  → order fills, position changes
  • Public channel   → ticker price for trailing-stop logic & auto partial-close
"""

import asyncio
import json
import time
import uuid
from typing import Callable, Optional
from loguru import logger

import websockets

from kucoin_client import KuCoinFuturesClient


# ── Health-check / REST-fallback константы ───────────────────────────────────
WS_READ_TIMEOUT       = 30   # сек тишины на recv() → форс реконнект
REST_FALLBACK_SILENCE = 10   # сек без WS-апдейтов по символу → REST-опрос
WATCHDOG_INTERVAL     = 5    # период проверки watchdog


class FuturesMonitor:
    def __init__(self, client: KuCoinFuturesClient):
        self.client   = client
        self._handlers: dict[str, list[Callable]] = {}
        self._price_cache: dict[str, float] = {}        # symbol → last price
        self._last_price_ts: dict[str, float] = {}      # symbol → ts последнего апдейта
        self._ws_private  = None
        self._ws_public   = None
        self._running     = False
        self._subscribed_tickers: set[str] = set()
        self._active_subscriptions: set[str] = set()     # уже подписанные на WS

    # ── Event bus ─────────────────────────────────────────────────────────────
    def on(self, event: str, handler: Callable):
        self._handlers.setdefault(event, []).append(handler)

    async def _emit(self, event: str, data: dict):
        for h in self._handlers.get(event, []):
            try:
                if asyncio.iscoroutinefunction(h):
                    await h(data)
                else:
                    h(data)
            except Exception as e:
                logger.error(f"Handler error [{event}]: {e}")

    def get_price(self, symbol: str) -> Optional[float]:
        return self._price_cache.get(symbol)

    # ── Main entry ────────────────────────────────────────────────────────────
    async def start(self, symbols: list[str] = None):
        self._running = True
        if symbols:
            self._subscribed_tickers = set(symbols)
            now = time.time()
            for s in symbols:
                self._last_price_ts.setdefault(s, now)
        try:
            await asyncio.gather(
                self._run_private_ws(),
                self._run_public_ws(),
                self._dynamic_subscribe_loop(),
                self._price_watchdog(),
            )
        except asyncio.CancelledError:
            logger.info("Monitor stopped.")

    async def stop(self):
        self._running = False

    def subscribe_ticker(self, symbol: str):
        """Dynamically add a symbol to price feed."""
        if symbol in self._subscribed_tickers:
            return
        self._subscribed_tickers.add(symbol)
        # Инициализируем timestamp, чтобы watchdog не выстрелил REST-запросом
        # сразу после подписки, а дал WS шанс прислать первую цену.
        self._last_price_ts[symbol] = time.time()
        # Если WS уже живой — подписываем прямо сейчас через asyncio task
        ws = self._ws_public
        if ws is not None and symbol not in self._active_subscriptions:
            asyncio.ensure_future(self._subscribe_single(ws, symbol))

    async def _subscribe_single(self, ws, symbol: str):
        """Подписать один символ на живой WS."""
        try:
            await ws.send(json.dumps({
                "id":       uuid.uuid4().hex,
                "type":     "subscribe",
                "topic":    f"/contractMarket/tickerV2:{symbol}",
                "response": True,
            }))
            self._active_subscriptions.add(symbol)
            logger.info(f"Live-subscribed to ticker: {symbol}")
        except Exception as e:
            logger.warning(f"Live subscribe failed for {symbol}: {e}")

    # ── Dynamic subscription loop ─────────────────────────────────────────────
    async def _dynamic_subscribe_loop(self):
        """
        Fallback: периодически проверяет, есть ли символы без подписки.
        Нужен на случай если subscribe_ticker был вызван в момент
        когда WS ещё не был готов или переподключался.
        """
        while self._running:
            try:
                await asyncio.sleep(2)  # проверяем раз в 2 секунды

                ws = self._ws_public
                if ws is None:
                    continue

                new_symbols = self._subscribed_tickers - self._active_subscriptions
                for symbol in new_symbols:
                    await self._subscribe_single(ws, symbol)
                    await asyncio.sleep(0.1)

            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error(f"Dynamic subscribe loop error: {e}")

    # ── Private WebSocket ─────────────────────────────────────────────────────
    async def _run_private_ws(self):
        while self._running:
            try:
                token_data = await self.client.get_private_ws_token()
                token      = token_data["token"]
                endpoint   = token_data["instanceServers"][0]["endpoint"]
                ping_interval = token_data["instanceServers"][0]["pingInterval"] // 1000

                url = f"{endpoint}?token={token}&connectId={uuid.uuid4().hex}"
                async with websockets.connect(url, ping_interval=None) as ws:
                    self._ws_private = ws
                    msg = json.loads(await ws.recv())
                    if msg.get("type") != "welcome":
                        raise RuntimeError("No welcome message from private WS")

                    for topic in [
                        "/contractMarket/tradeOrders",
                        "/contract/position",
                    ]:
                        await ws.send(json.dumps({
                            "id":             uuid.uuid4().hex,
                            "type":           "subscribe",
                            "topic":          topic,
                            "privateChannel": True,
                            "response":       True,
                        }))
                        await asyncio.sleep(0.2)

                    logger.info("Private WS connected & subscribed")
                    ping_task = asyncio.create_task(
                        self._pinger(ws, ping_interval)
                    )
                    try:
                        while True:
                            try:
                                raw = await asyncio.wait_for(
                                    ws.recv(), timeout=WS_READ_TIMEOUT
                                )
                            except asyncio.TimeoutError:
                                logger.warning(
                                    f"Private WS silent {WS_READ_TIMEOUT}s — "
                                    f"forcing reconnect"
                                )
                                raise
                            await self._handle_private_msg(json.loads(raw))
                    finally:
                        ping_task.cancel()

            except asyncio.CancelledError:
                raise   # propagate — don't swallow shutdown signal
            except Exception as e:
                if not self._running:
                    break
                logger.warning(f"Private WS error: {e}. Reconnecting in 5s…")
                await asyncio.sleep(5)

    async def _handle_private_msg(self, msg: dict):
        topic = msg.get("topic", "")
        data  = msg.get("data", {})

        # Логируем все сообщения по ордерам для отладки
        if "/tradeOrders" in topic:
            logger.info(f"WS tradeOrders: status={data.get('status')} "
                        f"type={data.get('type')} reason={data.get('reason')} "
                        f"symbol={data.get('symbol')} orderId={data.get('orderId','')[:16]}")

            status = data.get("status")
            otype  = data.get("type")
            reason = data.get("reason", "")

            # Исполнение ордера — любой done кроме cancelled/rejected
            if status == "done" and reason not in ("cancelledByUser", "rejectCancelled",
                                                    "liquidation", "closed"):
                await self._emit("order_filled", data)

            elif status == "match":
                await self._emit("order_filled", data)

            elif otype in ("stop", "trailing_stop") and status == "open":
                await self._emit("trailing_stop_placed", data)

            elif otype in ("stop", "trailing_stop") and status == "done":
                await self._emit("trailing_stop_triggered", data)

        elif "/position" in topic:
            qty = float(data.get("currentQty", 0))
            if qty != 0:
                await self._emit("position_opened", data)
            else:
                await self._emit("position_closed", data)

    # ── Public WebSocket ──────────────────────────────────────────────────────
    async def _run_public_ws(self):
        while self._running:
            try:
                token_data = await self.client.get_public_ws_token()
                token      = token_data["token"]
                endpoint   = token_data["instanceServers"][0]["endpoint"]
                ping_interval = token_data["instanceServers"][0]["pingInterval"] // 1000

                url = f"{endpoint}?token={token}&connectId={uuid.uuid4().hex}"
                async with websockets.connect(url, ping_interval=None) as ws:
                    self._ws_public = ws
                    msg = json.loads(await ws.recv())
                    if msg.get("type") != "welcome":
                        raise RuntimeError("No welcome from public WS")

                    # При (пере)подключении — сбрасываем active и подписываем заново
                    self._active_subscriptions.clear()
                    await self._subscribe_tickers(ws)
                    logger.info("Public WS connected & subscribed")

                    ping_task = asyncio.create_task(
                        self._pinger(ws, ping_interval)
                    )
                    try:
                        while True:
                            try:
                                raw = await asyncio.wait_for(
                                    ws.recv(), timeout=WS_READ_TIMEOUT
                                )
                            except asyncio.TimeoutError:
                                logger.warning(
                                    f"Public WS silent {WS_READ_TIMEOUT}s — "
                                    f"forcing reconnect"
                                )
                                raise
                            await self._handle_public_msg(json.loads(raw))
                    finally:
                        ping_task.cancel()
                        self._active_subscriptions.clear()

            except asyncio.CancelledError:
                raise   # propagate shutdown
            except Exception as e:
                if not self._running:
                    break
                logger.warning(f"Public WS error: {e}. Reconnecting in 5s…")
                await asyncio.sleep(5)

    async def _subscribe_tickers(self, ws):
        for symbol in list(self._subscribed_tickers):
            await ws.send(json.dumps({
                "id":       uuid.uuid4().hex,
                "type":     "subscribe",
                "topic":    f"/contractMarket/tickerV2:{symbol}",
                "response": True,
            }))
            self._active_subscriptions.add(symbol)
            await asyncio.sleep(0.1)

    async def _handle_public_msg(self, msg: dict):
        topic = msg.get("topic", "")
        data  = msg.get("data", {})

        if "/tickerV2:" in topic:
            symbol = topic.split(":")[-1]
            # Fallback bid → ask → price, чтобы не пропускать тики
            # с пустой стороной BBO (бывает на низколиквидных альтах).
            bid = float(data.get("bestBidPrice", 0) or 0)
            ask = float(data.get("bestAskPrice", 0) or 0)
            price = bid if bid > 0 else (ask if ask > 0
                                         else float(data.get("price", 0) or 0))
            if price > 0:
                self._price_cache[symbol] = price
                self._last_price_ts[symbol] = time.time()
                emit_data = dict(data)
                emit_data["symbol"] = symbol
                emit_data["price"]  = price
                await self._emit("price_update", emit_data)

    # ── Ping keepalive ────────────────────────────────────────────────────────
    async def _pinger(self, ws, interval: int):
        while True:
            await asyncio.sleep(interval)
            try:
                await ws.send(json.dumps({
                    "id":   uuid.uuid4().hex,
                    "type": "ping"
                }))
            except Exception:
                break

    # ── REST-fallback watchdog ────────────────────────────────────────────────
    async def _price_watchdog(self):
        """
        Если по символу нет WS-апдейтов дольше REST_FALLBACK_SILENCE сек —
        дёргаем mark-price через REST и сами эмитим price_update.
        Это страховка на случай залипания WS или потери подписки.
        В нормальном режиме (WS жив) запросов нет.
        """
        while self._running:
            try:
                await asyncio.sleep(WATCHDOG_INTERVAL)
                now = time.time()
                for symbol in list(self._subscribed_tickers):
                    last = self._last_price_ts.get(symbol, 0)
                    silence = now - last
                    if silence < REST_FALLBACK_SILENCE:
                        continue  # WS живой по этому символу
                    try:
                        price = await self.client.get_mark_price(symbol)
                        if price > 0:
                            self._price_cache[symbol] = price
                            self._last_price_ts[symbol] = time.time()
                            logger.warning(
                                f"REST fallback {symbol}: WS silent {int(silence)}s, "
                                f"mark={price}"
                            )
                            await self._emit("price_update", {
                                "symbol": symbol,
                                "price":  price,
                                "source": "rest_fallback",
                            })
                    except Exception as e:
                        logger.error(f"REST fallback failed for {symbol}: {e}")
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error(f"Price watchdog error: {e}")
