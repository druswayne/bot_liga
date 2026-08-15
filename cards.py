from aiogram.exceptions import TelegramBadRequest
from aiogram import Bot

import db
from keyboards import email_keyboard, ig_keyboard
from utils import format_card, format_ig_card

# token -> (chat_id, [message_id, ...])
view_packs: dict[str, tuple[int, list[int]]] = {}


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


async def refresh_ig_cards(bot: Bot, message_id: int) -> None:
    item = await db.get_ig_message(message_id)
    if not item:
        return

    notes = await db.get_ig_notifications(message_id)
    if item["deleted"]:
        for note in notes:
            try:
                await bot.delete_message(note["chat_id"], note["telegram_message_id"])
            except TelegramBadRequest:
                pass
        return

    text = format_ig_card(item, is_new=False)
    markup = ig_keyboard(item)

    for note in notes:
        try:
            await bot.edit_message_text(
                text,
                chat_id=note["chat_id"],
                message_id=note["telegram_message_id"],
                reply_markup=markup,
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            pass
