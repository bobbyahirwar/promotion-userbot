from pyrogram import Client
from config import API_ID, API_HASH, SESSION_STRING

userbot = Client(
    name="userbot",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)
