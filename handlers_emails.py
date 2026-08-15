from pathlib import Path
from uuid import uuid4

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InputMediaPhoto, Message

import db
from cards import refresh_cards
from config import ROLES, STATUS_IN_PROGRESS, STATUS_PROCESSED
from keyboards import CloseViewCB, EmailCB, close_view_keyboard, email_keyboard, reply_keyboard
from states import ReplyStates
from utils import format_card, split_text

router = Router()

# token -> (chat_id, [message_id, ...])
_view_packs: dict[str, tuple[int, list[int]]] = {}


@router.message(Command("list"))
async def cmd_list(message: Message):
    emails = await db.list_open_emails()
    if not emails:
        await message.answer("Писем пока нет.")
        return
    for email in reversed(emails):
        sent = await message.answer(
            format_card(email),
            reply_markup=email_keyboard(email),
            parse_mode="HTML",
        )
        await db.add_notification(email["id"], message.chat.id, sent.message_id)


@router.callback_query(EmailCB.filter())
async def on_email_action(
    query: CallbackQuery,
    callback_data: EmailCB,
    state: FSMContext,
    db_user: dict,
):
    email = await db.get_email(callback_data.eid)
    if not email or email["deleted"]:
        await query.answer("Письмо не найдено или удалено", show_alert=True)
        return

    act = callback_data.act
    if act == "view":
        await _view(query, email)
    elif act == "take":
        await _take(query, email, db_user)
    elif act == "release":
        await _release(query, email, db_user)
    elif act == "reply":
        await _start_reply(query, email, state, db_user)
    elif act == "delete":
        await _delete(query, email)
    else:
        await query.answer()


@router.callback_query(CloseViewCB.filter())
async def close_view(query: CallbackQuery, callback_data: CloseViewCB):
    pack = _view_packs.pop(callback_data.token, None)
    chat_id = query.message.chat.id
    message_ids = pack[1] if pack else [query.message.message_id]
    await query.answer()
    for message_id in message_ids:
        try:
            await query.bot.delete_message(chat_id, message_id)
        except TelegramBadRequest:
            pass


async def _view(query: CallbackQuery, email: dict) -> None:
    await query.answer()
    token = uuid4().hex[:8]
    sent_ids: list[int] = []
    body = email.get("body") or "Нет текста"
    chunks = split_text(f"Тема: {email.get('subject') or '(без темы)'}\n\n{body}")

    for index, chunk in enumerate(chunks):
        is_last_text = index == len(chunks) - 1
        sent = await query.message.answer(
            chunk,
            parse_mode=None,
            reply_markup=close_view_keyboard(token) if is_last_text else None,
        )
        sent_ids.append(sent.message_id)

    attachments = await db.get_attachments(email["id"])
    photos = [
        a for a in attachments if a["is_image"] and Path(a["path"]).exists()
    ]
    files = [
        a for a in attachments if not a["is_image"] and Path(a["path"]).exists()
    ]

    if photos:
        media = [
            InputMediaPhoto(media=FSInputFile(p["path"]), caption=p["filename"][:100])
            for p in photos[:10]
        ]
        try:
            group = await query.message.answer_media_group(media)
            sent_ids.extend(item.message_id for item in group)
        except TelegramBadRequest:
            for photo in photos:
                sent = await query.message.answer_photo(FSInputFile(photo["path"]))
                sent_ids.append(sent.message_id)
    for item in files:
        sent = await query.message.answer_document(
            FSInputFile(item["path"], filename=item["filename"])
        )
        sent_ids.append(sent.message_id)

    _view_packs[token] = (query.message.chat.id, sent_ids)


async def _take(query: CallbackQuery, email: dict, db_user: dict) -> None:
    if email["status"] == STATUS_PROCESSED:
        await query.answer("Письмо уже обработано", show_alert=True)
        return
    if email["status"] == STATUS_IN_PROGRESS:
        role = ROLES.get(email["taken_by"] or "")
        who = role["in_work"] if role else "уже взято"
        if email["taken_by"] == db_user["role"]:
            await query.answer("Вы уже взяли это письмо")
        else:
            await query.answer(who, show_alert=True)
        return
    ok = await db.take_email(email["id"], db_user["role"])
    if not ok:
        await query.answer("Не удалось взять письмо", show_alert=True)
        return
    await query.answer("Письмо взято в работу")
    await refresh_cards(query.bot, email["id"])


async def _release(query: CallbackQuery, email: dict, db_user: dict) -> None:
    if email["status"] != STATUS_IN_PROGRESS:
        await query.answer("Письмо не в работе", show_alert=True)
        return
    if email["taken_by"] != db_user["role"]:
        role = ROLES.get(email["taken_by"] or "")
        who = role["in_work"] if role else "уже в работе"
        await query.answer(f"{who}. Снять может только этот сотрудник.", show_alert=True)
        return
    ok = await db.release_email(email["id"], db_user["role"])
    if not ok:
        await query.answer("Не удалось вернуть письмо", show_alert=True)
        return
    await query.answer("Письмо снова в ожидании")
    await refresh_cards(query.bot, email["id"])


async def _start_reply(
    query: CallbackQuery, email: dict, state: FSMContext, db_user: dict
) -> None:
    if email["status"] == STATUS_PROCESSED:
        await query.answer("Письмо уже обработано", show_alert=True)
        return
    current = await state.get_state()
    if current == ReplyStates.collecting:
        await query.answer("Сначала завершите текущий ответ", show_alert=True)
        return
    if email["status"] != STATUS_IN_PROGRESS:
        await db.take_email(email["id"], db_user["role"])
        await refresh_cards(query.bot, email["id"])
    elif email["taken_by"] and email["taken_by"] != db_user["role"]:
        role = ROLES.get(email["taken_by"])
        who = role["in_work"] if role else "уже в работе"
        await query.answer(f"{who}. Ответить может только этот сотрудник.", show_alert=True)
        return

    await state.set_state(ReplyStates.collecting)
    await query.answer()
    prompt = await query.message.answer(
        "Отправьте текст ответа. Можно прикрепить файлы.\n"
        "Когда всё готово — нажмите «Отправить».",
        reply_markup=reply_keyboard(),
    )
    await state.update_data(
        email_id=email["id"],
        text_parts=[],
        files=[],
        chat_id=query.message.chat.id,
        message_ids=[prompt.message_id],
    )


async def _delete(query: CallbackQuery, email: dict) -> None:
    await db.soft_delete(email["id"])
    await query.answer("Письмо удалено из бота")
    await refresh_cards(query.bot, email["id"])
