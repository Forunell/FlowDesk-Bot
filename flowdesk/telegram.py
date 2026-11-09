"""Optional official Telegram transport. Imported only by the telegram command."""
import asyncio
import hashlib
import os

from .config import Settings
from .dialogue import options
from .storage import Database
from .telegram_service import deliver_one, receive


async def poll():
    settings = Settings.load()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise SystemExit("Set your own TELEGRAM_BOT_TOKEN in .env. Demo needs no token.")
    try:
        from aiogram import Bot, Dispatcher
        from aiogram.exceptions import TelegramAPIError
        from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup
    except ImportError:
        raise SystemExit("Install requirements-telegram.txt first.") from None
    database = Database(settings.database)
    bot, dispatcher = Bot(token=token), Dispatcher()
    admin = os.getenv("TELEGRAM_ADMIN_CHAT_ID", "")
    configured = os.getenv("TELEGRAM_ADMIN_CONFIRMED", "false").lower() == "true"
    # Verification is tied to this bot token and chat, never reused for another bot.
    verification_key = "telegram_admin:" + hashlib.sha256((token + ":" + admin).encode()).hexdigest()

    @dispatcher.message()
    async def handle(message: Message):
        if message.chat.type != "private" or not message.from_user:
            return
        if message.text == "/verify_admin":
            if admin and str(message.chat.id) == admin:
                with database.connect() as db:
                    db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (verification_key, "verified"))
                await message.answer("Доступ адміністратора підтверджено. Сповіщення вмикаються параметром TELEGRAM_ADMIN_CONFIRMED=true.")
            else:
                await message.answer("Цей чат не налаштовано як адміністративний.")
            return
        state, created = receive(database, message.chat.id, message.message_id, message.text)
        if state is None:
            return
        buttons = options(state)
        if state["step"] not in ("idle", "done", "confirm"):
            buttons.append("Скасувати")
        markup = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=label)] for label in buttons] or [[KeyboardButton(text="Скасувати")]], resize_keyboard=True)
        await message.answer(state["history"][-1]["text"], reply_markup=markup)
        if created and configured and admin:
            with database.connect() as db:
                verified = db.execute("SELECT 1 FROM metadata WHERE key=?", (verification_key,)).fetchone()
            if verified:
                try:
                    await bot.send_message(int(admin), f"Нова заявка FD-{created:04d}. Подробиці — у локальній адмін-панелі.")
                except (TelegramAPIError, ValueError):
                    # The request stays saved; no contacts or token in logs.
                    print("Admin notification unavailable; request remains saved.")

    async def send_queue():
        while True:
            if os.getenv("TELEGRAM_REPLIES_ENABLED", "false").lower() == "true":
                await deliver_one(database, bot)
            await asyncio.sleep(1)

    sender = None
    try:
        webhook = await bot.get_webhook_info()
        if webhook.url:
            raise SystemExit("A webhook is configured. Disable it explicitly before starting polling.")
        sender = asyncio.create_task(send_queue())
        await dispatcher.start_polling(bot, handle_as_tasks=False)
    finally:
        if sender:
            sender.cancel()
            try:
                await sender
            except asyncio.CancelledError:
                pass
        await bot.session.close()


def run():
    import logging
    logging.disable(logging.CRITICAL)  # HTTP clients may otherwise log token-bearing URLs.
    try:
        asyncio.run(poll())
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        raise SystemExit("Telegram connection failed. Check the local settings and network; no secret details logged.") from None
