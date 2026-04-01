"""
kucoin_patch.py
Запустить на сервере для добавления get_klines в kucoin_client.py:
  python3 kucoin_patch.py
"""

import sys

MARKER = "    # __ WebSocket token"
MARKER2 = "    # ── WebSocket token"

NEW_METHOD = '''
    async def get_klines(self, symbol: str, granularity: int = 1440,
                         count: int = 30) -> list:
        """
        Get klines (candles) for symbol.
        granularity: minutes (1, 5, 15, 30, 60, 120, 240, 480, 720, 1440, 10080)
                     1440 = 1 day
        count: number of candles
        Returns: list of [time, open, high, low, close, volume]
        """
        import time as _time
        now = int(_time.time())
        start = now - count * granularity * 60

        data = await self._request("GET", "/api/v1/kline/query", params={
            "symbol":      symbol,
            "granularity":  str(granularity),
            "from":         str(start * 1000),
            "to":           str(now * 1000),
        })
        if isinstance(data, list):
            return data
        return []

'''

# Read with multiple encoding fallbacks
for enc in ["utf-8", "cp1251", "latin-1"]:
    try:
        with open("kucoin_client.py", "r", encoding=enc) as f:
            code = f.read()
        print(f"Read with encoding: {enc}")
        break
    except (UnicodeDecodeError, UnicodeError):
        continue
else:
    print("ERROR: could not read kucoin_client.py with any encoding")
    sys.exit(1)

if "get_klines" in code:
    print("get_klines already exists, skipping")
else:
    # Try both marker variants (with unicode dash and with underscores)
    if MARKER2 in code:
        code = code.replace(MARKER2, NEW_METHOD + MARKER2)
        print("Inserted before marker (unicode dash)")
    elif MARKER in code:
        code = code.replace(MARKER, NEW_METHOD + MARKER)
        print("Inserted before marker (underscores)")
    else:
        # Fallback: insert before get_private_ws_token
        code = code.replace(
            "    async def get_private_ws_token",
            NEW_METHOD + "    async def get_private_ws_token"
        )
        print("Inserted before get_private_ws_token (fallback)")

    # Always write as utf-8
    with open("kucoin_client.py", "w", encoding="utf-8") as f:
        f.write(code)
    print("OK: get_klines added to kucoin_client.py")

# Verify
import ast
ast.parse(open("kucoin_client.py", encoding="utf-8").read())
print("Syntax OK")
