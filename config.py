import os
import sys
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
