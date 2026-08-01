from telegram.ext import Application
from telegram.request import HTTPXRequest
from config import BOT_TOKEN

_request = HTTPXRequest(
    connection_pool_size=8,
    connect_timeout=30.0,
    read_timeout=30.0,
    write_timeout=30.0,
    pool_timeout=30.0,
)

bot_app: Application = (
    Application.builder()
    .token(BOT_TOKEN)
    .request(_request)
    .build()
)
