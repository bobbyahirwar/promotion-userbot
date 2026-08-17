import os
import sys
import base64
import struct
from dotenv import load_dotenv

load_dotenv()

REQUIRED_VARS = [
    "BOT_TOKEN",
    "API_ID",
    "API_HASH",
    "SESSION_STRING",
    "OWNER_ID",
    "MONGO_URI",
]

missing = [var for var in REQUIRED_VARS if not os.getenv(var)]
if missing:
    print(f"❌ Missing required environment variables: {', '.join(missing)}")
    sys.exit(1)

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
SESSION_STRING = os.getenv("SESSION_STRING")
OWNER_ID = int(os.getenv("OWNER_ID"))
MONGO_URI = os.getenv("MONGO_URI")

# Seconds between promotion cycles. Override via PROMOTION_INTERVAL_SECONDS
# environment variable; defaults to 60.
PROMOTION_INTERVAL_SECONDS = int(os.getenv("PROMOTION_INTERVAL_SECONDS", 60))

# Conservative delay between promotional sends to different groups.
# If the values are missing, invalid, or inconsistent, fall back to the safe
# default 15-30 second window so the bot remains stable and predictable.
_DEFAULT_PROMOTION_MIN_DELAY_SECONDS = 15
_DEFAULT_PROMOTION_MAX_DELAY_SECONDS = 30

try:
    _raw_min_delay = int(os.getenv("PROMOTION_MIN_DELAY_SECONDS", _DEFAULT_PROMOTION_MIN_DELAY_SECONDS))
    _raw_max_delay = int(os.getenv("PROMOTION_MAX_DELAY_SECONDS", _DEFAULT_PROMOTION_MAX_DELAY_SECONDS))
except (TypeError, ValueError):
    _raw_min_delay = _DEFAULT_PROMOTION_MIN_DELAY_SECONDS
    _raw_max_delay = _DEFAULT_PROMOTION_MAX_DELAY_SECONDS

if _raw_min_delay < 1 or _raw_max_delay < _raw_min_delay:
    PROMOTION_MIN_DELAY_SECONDS = _DEFAULT_PROMOTION_MIN_DELAY_SECONDS
    PROMOTION_MAX_DELAY_SECONDS = _DEFAULT_PROMOTION_MAX_DELAY_SECONDS
else:
    PROMOTION_MIN_DELAY_SECONDS = _raw_min_delay
    PROMOTION_MAX_DELAY_SECONDS = _raw_max_delay

# Global cooldown after Telegram FloodWait. This is intentionally separate from
# the per-group rate-limit added in Part 1 and is enforced as a safety stop.
_DEFAULT_PROMOTION_COOLDOWN_SECONDS = 1800
try:
    PROMOTION_COOLDOWN_SECONDS = int(os.getenv("PROMOTION_COOLDOWN_SECONDS", _DEFAULT_PROMOTION_COOLDOWN_SECONDS))
except (TypeError, ValueError):
    PROMOTION_COOLDOWN_SECONDS = _DEFAULT_PROMOTION_COOLDOWN_SECONDS

if PROMOTION_COOLDOWN_SECONDS < 1:
    PROMOTION_COOLDOWN_SECONDS = _DEFAULT_PROMOTION_COOLDOWN_SECONDS

# Consecutive temporary send failures that trigger a global promotion cooldown.
_DEFAULT_PROMOTION_MAX_CONSECUTIVE_ERRORS = 5
try:
    PROMOTION_MAX_CONSECUTIVE_ERRORS = int(
        os.getenv("PROMOTION_MAX_CONSECUTIVE_ERRORS", _DEFAULT_PROMOTION_MAX_CONSECUTIVE_ERRORS)
    )
except (TypeError, ValueError):
    PROMOTION_MAX_CONSECUTIVE_ERRORS = _DEFAULT_PROMOTION_MAX_CONSECUTIVE_ERRORS

if PROMOTION_MAX_CONSECUTIVE_ERRORS < 1:
    PROMOTION_MAX_CONSECUTIVE_ERRORS = _DEFAULT_PROMOTION_MAX_CONSECUTIVE_ERRORS

# End-of-cycle safety circuit breaker. If the failure rate is too high across a
# sufficiently large sample, promotion is paused until the operator intervenes.
_DEFAULT_PROMOTION_MAX_FAILURE_RATE = 0.50
_DEFAULT_PROMOTION_MIN_GROUPS_FOR_FAILURE_RATE = 10

try:
    PROMOTION_MAX_FAILURE_RATE = float(
        os.getenv("PROMOTION_MAX_FAILURE_RATE", _DEFAULT_PROMOTION_MAX_FAILURE_RATE)
    )
except (TypeError, ValueError):
    PROMOTION_MAX_FAILURE_RATE = _DEFAULT_PROMOTION_MAX_FAILURE_RATE

if PROMOTION_MAX_FAILURE_RATE < 0:
    PROMOTION_MAX_FAILURE_RATE = _DEFAULT_PROMOTION_MAX_FAILURE_RATE

try:
    PROMOTION_MIN_GROUPS_FOR_FAILURE_RATE = int(
        os.getenv(
            "PROMOTION_MIN_GROUPS_FOR_FAILURE_RATE",
            _DEFAULT_PROMOTION_MIN_GROUPS_FOR_FAILURE_RATE,
        )
    )
except (TypeError, ValueError):
    PROMOTION_MIN_GROUPS_FOR_FAILURE_RATE = _DEFAULT_PROMOTION_MIN_GROUPS_FOR_FAILURE_RATE

if PROMOTION_MIN_GROUPS_FOR_FAILURE_RATE < 1:
    PROMOTION_MIN_GROUPS_FOR_FAILURE_RATE = _DEFAULT_PROMOTION_MIN_GROUPS_FOR_FAILURE_RATE

# ── Validate SESSION_STRING format ────────────────────────────────────────────
# Pyrogram 2.0.106 accepts 271 bytes (new), 267 bytes (old 64-bit),
# or 263 bytes (old 32-bit) after base64url-decoding.
_VALID_SIZES = {271, 267, 263}
try:
    _decoded = base64.urlsafe_b64decode(
        SESSION_STRING + "=" * (-len(SESSION_STRING) % 4)
    )
    if len(_decoded) not in _VALID_SIZES:
        print(
            f"❌ SESSION_STRING is incompatible with Pyrogram 2.0.106.\n"
            f"   Decoded size: {len(_decoded)} bytes "
            f"(accepted: {sorted(_VALID_SIZES)}).\n"
            f"   Please regenerate it with Pyrogram 2.0.106:\n\n"
            f"     from pyrogram import Client\n"
            f"     import asyncio\n\n"
            f"     async def gen():\n"
            f"         async with Client('tmp', api_id={os.getenv('API_ID')},\n"
            f"                           api_hash='YOUR_API_HASH') as app:\n"
            f"             print(await app.export_session_string())\n\n"
            f"     asyncio.run(gen())\n"
        )
        sys.exit(1)
except Exception as e:
    print(f"❌ SESSION_STRING could not be decoded: {e}")
    sys.exit(1)
