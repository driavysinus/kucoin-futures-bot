"""
kucoin_client.py
Thin async-friendly wrapper over the KuCoin Futures REST API.
Uses httpx (no C++ compiler needed on Windows).
"""

import time
import hmac
import hashlib
import base64
import json
import uuid
import asyncio
from typing import Optional
from loguru import logger

import httpx
import config


BASE_URL = "https://api-futures.kucoin.com"


def _hmac_b64(secret: bytes, msg: bytes) -> str:
    """HMAC-SHA256 → base64 string."""
    return base64.b64encode(
        hmac.new(secret, msg, digestmod=hashlib.sha256).digest()
    ).decode()


def _sign(secret: str, passphrase: str, api_key: str,
          method: str, path: str, body: str = "") -> dict:
    """Generate KuCoin Futures auth headers (API key version 2)."""
    ts      = str(int(time.time() * 1000))
    sig_str = ts + method.upper() + path + (body or "")
    secret_b = secret.encode("utf-8")

    sig    = _hmac_b64(secret_b, sig_str.encode("utf-8"))
    pp_sig = _hmac_b64(secret_b, passphrase.encode("utf-8"))

    return {
        "KC-API-KEY":         api_key,
        "KC-API-SIGN":        sig,
        "KC-API-TIMESTAMP":   ts,
        "KC-API-PASSPHRASE":  pp_sig,
        "KC-API-KEY-VERSION": "2",
        "Content-Type":       "application/json",
    }


class KuCoinFuturesClient:
    def __init__(self):
        self.key        = config.KUCOIN_API_KEY.strip()
        self.secret     = config.KUCOIN_API_SECRET.strip()
        self.passphrase = config.KUCOIN_API_PASSPHRASE.strip()
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=BASE_URL,
                timeout=10.0
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _request(self, method: str, path: str, params: dict = None,
                       body: dict = None) -> dict:
        client   = await self._get_client()
        body_str = json.dumps(body, separators=(",", ":")) if body else ""

        # Build full path WITH query string — KuCoin signs the full path
        full_path = path
        if params:
            full_path += "?" + "&".join(f"{k}={v}" for k, v in params.items())

        # Sign using full_path (including query string)
        headers = _sign(self.secret, self.passphrase, self.key,
                        method, full_path, body_str)

        for attempt in range(3):
            try:
                resp = await client.request(
                    method, full_path, headers=headers,
                    content=body_str or None
                )
                data = resp.json()
                if data.get("code") != "200000":
                    raise RuntimeError(
                        f"KuCoin API error {data.get('code')}: {data.get('msg')}"
                    )
                return data.get("data", {})
            except RuntimeError:
                raise   # API errors — don't retry, re-raise immediately
            except Exception as e:
                if attempt == 2:
                    raise
                logger.warning(f"Request attempt {attempt+1} failed: {e}. Retrying…")
                await asyncio.sleep(1.5 ** attempt)

    # ── Account ───────────────────────────────────────────────────────────────
    async def get_account_overview(self, currency: str = "USDT") -> dict:
        return await self._request("GET", "/api/v1/account-overview",
                                   params={"currency": currency})

    # ── Positions ─────────────────────────────────────────────────────────────
    async def get_positions(self) -> list:
        data = await self._request("GET", "/api/v1/positions")
        return data if isinstance(data, list) else []

    async def get_position(self, symbol: str) -> Optional[dict]:
        positions = await self.get_positions()
        for p in positions:
            if p.get("symbol") == symbol and float(p.get("currentQty", 0)) != 0:
                return p
        return None

    # ── Orders ────────────────────────────────────────────────────────────────
    async def place_limit_order(self, symbol: str, side: str, size: float,
                                price: float, leverage: int,
                                client_oid: str = None) -> dict:
        body = {
            "clientOid":   client_oid or uuid.uuid4().hex,
            "symbol":      symbol,
            "side":        side,
            "type":        "limit",
            "price":       str(price),
            "size":        int(size),
            "leverage":    str(leverage),
            "timeInForce": "GTC",
            "marginMode":  config.MARGIN_MODE,
        }
        return await self._request("POST", "/api/v1/orders", body=body)

    async def place_market_order(self, symbol: str, side: str, size: float,
                                 leverage: int) -> dict:
        body = {
            "clientOid":  uuid.uuid4().hex,
            "symbol":     symbol,
            "side":       side,
            "type":       "market",
            "size":       int(size),
            "leverage":   str(leverage),
            "marginMode": config.MARGIN_MODE,
        }
        return await self._request("POST", "/api/v1/orders", body=body)

    async def place_trailing_stop_order(self, symbol: str, side: str,
                                        size: float, callback_rate: float,
                                        leverage: int,
                                        stop_price: float = None) -> dict:
        if not stop_price or stop_price <= 0:
            stop_price = await self.get_mark_price(symbol)

        stop_direction = "down" if side == "sell" else "up"

        body = {
            "clientOid":     uuid.uuid4().hex,
            "symbol":        symbol,
            "side":          side,
            "type":          "market",
            "size":          int(size),
            "leverage":      str(leverage),
            "stop":          stop_direction,
            "stopPriceType": "TP",
            "stopPrice":     str(stop_price),
            "trailingStop":  True,
            "callbackRate":  float(callback_rate),
            "marginMode":    config.MARGIN_MODE,
            "reduceOnly":    True,
        }
        logger.info(f"Trailing stop body → {body}")
        return await self._request("POST", "/api/v1/orders", body=body)

    async def place_stop_market_entry(self, symbol: str, side: str, size: float,
                                      stop_price: float, stop_direction: str,
                                      leverage: int) -> dict:
        """
        Стоп-маркет ордер на ВХОД в позицию (не reduceOnly).
        stop_direction: "up"   — активируется когда цена растёт до stop_price (buy)
                        "down" — активируется когда цена падает до stop_price (sell)
        """
        body = {
            "clientOid":     uuid.uuid4().hex,
            "symbol":        symbol,
            "side":          side,
            "type":          "market",
            "size":          int(size),
            "leverage":      str(leverage),
            "stop":          stop_direction,
            "stopPriceType": "TP",
            "stopPrice":     str(stop_price),
            "marginMode":    config.MARGIN_MODE,
        }
        logger.info(f"Stop market entry body → {body}")
        return await self._request("POST", "/api/v1/orders", body=body)

    async def place_stop_market_close(self, symbol: str, side: str, size: float,
                                      stop_price: float, stop_direction: str,
                                      leverage: int) -> dict:
        """
        Стоп-маркет ордер на ЗАКРЫТИЕ позиции (reduceOnly).
        Используется для тейк-профита / пореза как реального ордера на бирже.
        stop_direction: "up"   — активируется когда цена растёт (закрытие шорта)
                        "down" — активируется когда цена падает (закрытие лонга)
        """
        body = {
            "clientOid":     uuid.uuid4().hex,
            "symbol":        symbol,
            "side":          side,
            "type":          "market",
            "size":          int(size),
            "leverage":      str(leverage),
            "stop":          stop_direction,
            "stopPriceType": "TP",
            "stopPrice":     str(stop_price),
            "marginMode":    config.MARGIN_MODE,
            "reduceOnly":    True,
        }
        logger.info(f"Stop market close body → {body}")
        return await self._request("POST", "/api/v1/orders", body=body)

    async def place_stop_limit_order(self, symbol: str, side: str, size: float,
                                     stop_price: float, limit_price: float,
                                     leverage: int) -> dict:
        """Стоп-лимит ордер — используется для стопа в безубыток."""
        body = {
            "clientOid":     uuid.uuid4().hex,
            "symbol":        symbol,
            "side":          side,
            "type":          "limit",
            "size":          int(size),
            "price":         str(limit_price),
            "stop":          "down" if side == "sell" else "up",
            "stopPriceType": "TP",
            "stopPrice":     str(stop_price),
            "leverage":      str(leverage),
            "timeInForce":   "GTC",
            "marginMode":    config.MARGIN_MODE,
            "reduceOnly":    True,
        }
        logger.info(f"Stop limit order body → {body}")
        return await self._request("POST", "/api/v1/orders", body=body)

    async def cancel_order(self, order_id: str) -> dict:
        return await self._request("DELETE", f"/api/v1/orders/{order_id}")

    async def cancel_all_orders(self, symbol: str) -> dict:
        return await self._request("DELETE", "/api/v1/orders",
                                   params={"symbol": symbol})

    async def get_open_orders(self, symbol: str = None) -> list:
        params = {"status": "active"}
        if symbol:
            params["symbol"] = symbol
        data = await self._request("GET", "/api/v1/orders", params=params)
        return data.get("items", [])

    async def get_order(self, order_id: str) -> dict:
        return await self._request("GET", f"/api/v1/orders/{order_id}")

    # ── Market data ───────────────────────────────────────────────────────────
    async def get_ticker(self, symbol: str) -> dict:
        return await self._request("GET", "/api/v1/ticker",
                                   params={"symbol": symbol})

    async def get_mark_price(self, symbol: str) -> float:
        data = await self._request("GET", f"/api/v1/mark-price/{symbol}/current")
        return float(data.get("value", 0))

    async def get_contract_info(self, symbol: str) -> dict:
        """
        Returns contract details including multiplier.
        multiplier = USD value of 1 contract (BTC: 0.001, ETH: 0.01, etc.)
        """
        return await self._request("GET", f"/api/v1/contracts/{symbol}")

    async def usdt_to_contracts(self, symbol: str, usdt_amount: float,
                                price: float = None) -> tuple[int, float, float]:
        """
        Convert USDT amount to number of contracts.
        Formula: contracts = usdt_amount / (mark_price × multiplier)
        Returns: (contracts: int, actual_price: float, multiplier: float)
        """
        info       = await self.get_contract_info(symbol)
        multiplier = float(info.get("multiplier", 1))

        if price is None or price <= 0:
            price = await self.get_mark_price(symbol)

        contracts     = usdt_amount / (price * multiplier)
        contracts_int = max(1, int(contracts))

        logger.debug(
            f"usdt_to_contracts: {usdt_amount} USDT / "
            f"({price} × {multiplier}) = {contracts:.3f} → {contracts_int} contracts"
        )
        return contracts_int, price, multiplier

    # ── WebSocket token ───────────────────────────────────────────────────────
    async def get_private_ws_token(self) -> dict:
        return await self._request("POST", "/api/v1/bullet-private")

    async def get_public_ws_token(self) -> dict:
        return await self._request("POST", "/api/v1/bullet-public")

