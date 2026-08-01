# Python Telegram Bot Project

## Overview
A Python project scaffolded with Pyrogram (Telegram MTProto client), Motor (async MongoDB driver), and python-dotenv for environment configuration.

## Stack
- **Python 3.11**
- **Pyrogram 2.0.106** — Telegram bot/client framework (MTProto)
- **TgCrypto** — fast cryptography addon for Pyrogram
- **Motor 3.4.0** — async MongoDB driver
- **python-dotenv 1.0.1** — loads `.env` variables

## Project Files
| File | Purpose |
|------|---------|
| `main.py` | Entry point — initialises Pyrogram client and Motor connection |
| `config.py` | Reads all settings from environment variables via dotenv |
| `requirements.txt` | Pinned Python dependencies |
| `runtime.txt` | Specifies Python 3.11 runtime |

## Running the Project
1. Create a `.env` file (see below) with your credentials.
2. Run: `python main.py`

## Required Environment Variables
Create a `.env` file in the project root:
```
API_ID=your_telegram_api_id
API_HASH=your_telegram_api_hash
BOT_TOKEN=your_bot_token
MONGO_URI=mongodb://localhost:27017
DB_NAME=mybot
```

Get `API_ID` and `API_HASH` from https://my.telegram.org.
Get `BOT_TOKEN` from @BotFather on Telegram.

## User Preferences
- Minimal scaffolding — no bot logic, just runnable project structure.
- Files: main.py, config.py, requirements.txt, runtime.txt only.
