from telegram import Bot

from app.config import settings


async def send_alert(chat_id: str, message: str) -> None:
    try:
        bot = Bot(token=settings.telegram_bot_token)
        await bot.send_message(chat_id=chat_id, text=message)
    except Exception as e:
        print(f"[Alert] Failed to send telegram message: {e}")


async def critical(message: str) -> None:
    if settings.telegram_bot_token == "dummy":
        print(f"[Alert CRITICAL] {message}")
        return
    await send_alert(settings.telegram_chat_id_critical, f"🔴 CRITICAL\n{message}")


async def warn(message: str) -> None:
    if settings.telegram_bot_token == "dummy":
        print(f"[Alert WARN] {message}")
        return
    await send_alert(settings.telegram_chat_id_warn, f"🟡 WARN\n{message}")
