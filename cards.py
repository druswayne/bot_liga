from aiogram.exceptions import TelegramBadRequest
from aiogram import Bot

import db
from keyboards import email_keyboard
from utils import format_card


async def refresh_cards(bot: Bot, email_id: int) -> None:
    email = await db.get_email(email_id)
    if not email:
        return

    notes = await db.get_notifications(email_id)
    if email["deleted"]:
        for note in notes:
            try:
                await bot.delete_message(note["chat_id"], note["message_id"])
            except TelegramBadRequest:
                pass
        return

    text = format_card(email, is_new=False)
    markup = email_keyboard(email)

    for note in notes:
        try:
            await bot.edit_message_text(
                text,
                chat_id=note["chat_id"],
                message_id=note["message_id"],
                reply_markup=markup,
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            pass
