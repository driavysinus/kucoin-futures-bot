"""
kucoin_patch.py
Запустить на сервере для добавления get_klines в kucoin_client.py:
  python3 kucoin_patch.py
"""
import re
import ast

MARKER = "    # ── WebSocket token"
NEW_METHOD = '''
    async def get_klines(self, symbol: str, granularity: int = 1440,
                         count: int = 30) -> list:
        """
        Получить свечи (klines) для символа.
        granularity: минуты (1, 5, 15, 30, 60, 120, 240, 480, 720, 1440, 10080)
                     1440 = 1 день
        count: количество свечей
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
        # data — список списков: [[time, open, high, low, close, volume], ...]
        if isinstance(data, list):
            return data
        return []
'''

with open("kucoin_client.py", "r", encoding="utf-8") as f:
    code = f.read()

if "get_klines" in code:
    print("get_klines already exists, skipping")
else:
    code = code.replace(MARKER, NEW_METHOD + MARKER)
    with open("kucoin_client.py", "w", encoding="utf-8") as f:
        f.write(code)
    print("OK: get_klines added to kucoin_client.py")

# Verify
ast.parse(open("kucoin_client.py", encoding="utf-8").read())
print("Syntax OK")