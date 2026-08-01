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
