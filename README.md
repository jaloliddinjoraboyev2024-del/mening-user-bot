# Telegram User Bot

Telegram userbot with a button-driven management interface, QR login, session locking, selected-chat workflows, safe one-time bulk messaging, message cleanup, and connection/presence monitoring.

## Features
- QR login for a new Telegram user session
- Hidden terminal prompt for Telegram 2FA
- Professional inline-button navigation
- Chat categories, pagination and persistent selection
- Selected chats remain selected after cleanup/send operations
- One-time sequential messaging to selected chats with stop control
- Delete own messages with sender-ID fallback
- Session lock and graceful shutdown
- Background connection/presence refresh while the process is running
- SQLite state and one-time confirmation tokens

## Setup
1. Copy `.env.example` to `.env` and fill in your Telegram API credentials and bot token.
2. Create and activate a virtual environment.
3. Install dependencies with `pip install -r requirements.txt`.
4. Run `python main.py`.
5. On the first user-session login, scan the QR shown by the control bot and enter 2FA in the terminal if enabled.

## Security
Never commit `.env`, Telegram `.session` files, database files, logs, QR images, or other credentials. These are ignored by `.gitignore`.
