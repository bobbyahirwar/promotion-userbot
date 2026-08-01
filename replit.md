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
The workflow **Start application** runs `python -u main.py` automatically.

## Required Secrets (set via Replit Secrets)
| Variable | Source |
|---|---|
| `BOT_TOKEN` | @BotFather on Telegram |
| `API_ID` | https://my.telegram.org |
| `API_HASH` | https://my.telegram.org |
| `SESSION_STRING` | Generated with Pyrogram on the userbot account |
| `OWNER_ID` | Your Telegram numeric user ID |
| `MONGO_URI` | MongoDB Atlas connection string |

## Notes
- `main.py` was missing `asyncio.run(main())` — added to fix the silent exit on startup.
- `SESSION_STRING` must be a valid Pyrogram 2.0.106 session (271-byte decoded); see `config.py` for regeneration instructions if it fails validation.
