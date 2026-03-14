import os
from dotenv import load_dotenv

load_dotenv()

# ── KuCoin ──────────────────────────────────────────────────────────────────
KUCOIN_API_KEY        = os.getenv("KUCOIN_API_KEY", "")
KUCOIN_API_SECRET     = os.getenv("KUCOIN_API_SECRET", "")
KUCOIN_API_PASSPHRASE = os.getenv("KUCOIN_API_PASSPHRASE", "")

# ── Telegram ─────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN    = os.getenv("TELEGRAM_BOT_TOKEN", "")
_raw_users            = os.getenv("TELEGRAM_ALLOWED_USERS", "")
TELEGRAM_ALLOWED_USERS = [int(u.strip()) for u in _raw_users.split(",") if u.strip()]

# ── Trading defaults ──────────────────────────────────────────────────────────
DEFAULT_LEVERAGE           = int(os.getenv("DEFAULT_LEVERAGE", 10))
DEFAULT_PARTIAL_CLOSE_PCT  = float(os.getenv("DEFAULT_PARTIAL_CLOSE_PCT", 50))
DEFAULT_TRAILING_STOP_PCT  = float(os.getenv("DEFAULT_TRAILING_STOP_PCT", 1.5))
DEFAULT_PROFIT_TRIGGER_PCT = float(os.getenv("DEFAULT_PROFIT_TRIGGER_PCT", 2.0))

# ── Sanity check ──────────────────────────────────────────────────────────────
def validate():
    missing = []
    if not KUCOIN_API_KEY:        missing.append("KUCOIN_API_KEY")
    if not KUCOIN_API_SECRET:     missing.append("KUCOIN_API_SECRET")
    if not KUCOIN_API_PASSPHRASE: missing.append("KUCOIN_API_PASSPHRASE")
    if not TELEGRAM_BOT_TOKEN:    missing.append("TELEGRAM_BOT_TOKEN")
    if missing:
        raise EnvironmentError(f"Missing required env vars: {', '.join(missing)}")
