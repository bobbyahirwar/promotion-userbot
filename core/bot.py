from telegram.ext import Application
from config import BOT_TOKEN

bot_app: Application = Application.builder().token(BOT_TOKEN).build()
