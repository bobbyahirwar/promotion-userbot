import asyncio
import motor.motor_asyncio
from pyrogram import Client
from config import API_ID, API_HASH, BOT_TOKEN, MONGO_URI, DB_NAME

# MongoDB client
mongo_client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
db = mongo_client[DB_NAME]

# Pyrogram client
app = Client(
    "bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)


async def main():
    print("Starting bot...")
    await app.start()
    print("Bot is running.")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
