# Telegram bot setup

This bot issues license JSON files for AI DAILY VIDEO CREATOR.

## 1) Create the Telegram bot token

Open `@BotFather` in Telegram and create a new bot.

Copy the bot token into your local `.env` file as:

```env
BOT_TOKEN=123456789:ABCDEF_your_bot_token_here
```

Do not commit a real `.env` file.

## 2) Create signing keys

On the admin machine, run:

```bash
python generate_license.py --init-keys
```

This creates:

- `private_key.pem` on the admin machine
- `public_key.pem` for distribution with the app

Do not commit `private_key.pem`.

## 3) Install dependencies

```bash
pip install -r requirements.txt
```

## 4) Configure `.env`

Copy `.env.example` to `.env` and fill in:

- `BOT_TOKEN`
- `ADMIN_IDS`
- `PRIVATE_KEY_PATH`
- `LICENSE_DB_PATH`
- `LICENSE_OUTPUT_DIR`
- `BANK_*` values if you use bank payment flow

## 5) Run the bot

```bash
python telegram_license_bot.py
```

## 6) Expected commands

- `/license <MACHINE_ID>`
- `/order <ORDER_ID>`
- Admin `/paid <ORDER_ID>`
- Admin `/grant_free <telegram_user_id> <machine_id>`
- Admin `/grant_permanent <telegram_user_id> <machine_id>`

## 7) License plans

- Trial: `trial_10d`
- Paid annual: `paid_365d`
- Lifetime: `permanent`

The app accepts the signed JSON produced by this bot and binds it to the target Machine ID.
