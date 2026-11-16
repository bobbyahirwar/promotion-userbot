import os
import sys
from dotenv import load_dotenv

load_dotenv()

REQUIRED_VARS = [
    "BOT_TOKEN",
    "API_ID",
    "API_HASH",
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
# Keep supporting deployments that still provide the old single-account
# session. It is imported into the accounts collection at startup.
SESSION_STRING = os.getenv("SESSION_STRING") or os.getenv("USERBOT_SESSION_STRING")
OWNER_ID = int(os.getenv("OWNER_ID"))
MONGO_URI = os.getenv("MONGO_URI")

# Seconds between promotion cycles. Override via PROMOTION_INTERVAL_SECONDS
# environment variable; defaults to 600 (10 minutes).
PROMOTION_INTERVAL_SECONDS = int(os.getenv("PROMOTION_INTERVAL_SECONDS", 600))

# Maximum random variation on either side of the configured cycle interval.
PROMOTION_INTERVAL_VARIATION_SECONDS = int(
    os.getenv("PROMOTION_INTERVAL_VARIATION_SECONDS", 60)
)

PROMOTION_DECORATIVE_VARIATION = os.getenv(
    "PROMOTION_DECORATIVE_VARIATION", "true"
).strip().lower() in {"1", "true", "yes", "on"}

# Promotion safety settings use built-in defaults so Render does not need any
# new environment variables for these protections. Keep the existing required
# app env vars unchanged.
PROMOTION_MIN_DELAY_SECONDS = 15
PROMOTION_MAX_DELAY_SECONDS = 30
PROMOTION_COOLDOWN_SECONDS = 1800
PROMOTION_MAX_CONSECUTIVE_ERRORS = 5
PROMOTION_MAX_FAILURE_RATE = 0.50
PROMOTION_MIN_GROUPS_FOR_FAILURE_RATE = 10
PROMOTION_MAX_RECOVERY_ATTEMPTS = 3
PROMOTION_MAX_CONTINUOUS_RUNTIME_SECONDS = 4 * 60 * 60
PROMOTION_REST_DURATION_SECONDS = 60 * 60
PROMOTION_LONG_FLOODWAIT_THRESHOLD_SECONDS = int(
    os.getenv("PROMOTION_LONG_FLOODWAIT_THRESHOLD_SECONDS", 600)
)
EXTRA_LONG_FLOODWAIT_BUFFER_SECONDS = int(
    os.getenv("EXTRA_LONG_FLOODWAIT_BUFFER_SECONDS", 300)
)
