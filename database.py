import sys
import motor.motor_asyncio
from config import MONGO_URI

_client: motor.motor_asyncio.AsyncIOMotorClient = None
db = None


async def init_db():
    global _client, db
    try:
        _client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
        # Verify the connection is reachable
        await _client.admin.command("ping")
        db = _client.get_default_database(default="promotionbot")
        print("✅ MongoDB Connected")
    except Exception as e:
        print(f"❌ MongoDB failed to connect: {e}")
        sys.exit(1)


def get_db():
    return db
