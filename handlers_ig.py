from pathlib import Path
from uuid import uuid4

from aiogram import Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, FSInputFile, InputMediaPhoto, Message

import db
from cards import refresh_ig_cards, view_packs
from config import ROLES, STATUS_IN_PROGRESS, STATUS_PROCESSED
from ig_client import submit_code
from keyboards import IgCB, close_view_keyboard, ig_keyboard, reply_keyboard
from states import ReplyStates
from utils import format_ig_card, split_text

router = Router()


@router.message(Command("ig_code"))
async def cmd_ig_code(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Использование: /ig_code 123456")
        return
    code = parts[1].strip()
    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    submit_code(code)
    await message.answer("Код Instagram принят.")


@router.callback_query(IgCB.filter())
async def on_ig_action(
    query: CallbackQuery,
    callback_data: IgCB,
    state: FSMContext,
    db_user: dict,
):
    item = await db.get_ig_message(callback_data.eid)
    if not item or item["deleted"]:
        await query.answer("Сообщение не найдено или удалено", show_alert=True)
        return

    act = callback_data.act
    if act == "view":
        await _view(query, item)
    elif act == "take":
        await _take(query, item, db_user)
    elif act == "release":
        await _release(query, item, db_user)
    elif act == "reply":
        await _start_reply(query, item, state, db_user)
    elif act == "delete":
        await _delete(query, item)
    else:
        await query.answer()


async def _view(query: CallbackQuery, item: dict) -> None:
    await query.answer()
    token = uuid4().hex[:8]
    sent_ids: list[int] = []
    sender = item.get("sender_username") or item.get("sender_name") or "неизвестно"
    if item.get("sender_username"):
        sender = f"@{item['sender_username']}"
    body = item.get("body") or "Нет текста"
    chunks = split_text(f"От: {sender}\n\n{body}")

    for index, chunk in enumerate(chunks):
        is_last_text = index == len(chunks) - 1
        sent = await query.message.answer(
            chunk,
            parse_mode=None,
            reply_markup=close_view_keyboard(token) if is_last_text else None,
        )
        sent_ids.append(sent.message_id)

    attachments = await db.get_ig_attachments(item["id"])
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
            sent_ids.extend(media_msg.message_id for media_msg in group)
        except TelegramBadRequest:
            for photo in photos:
                sent = await query.message.answer_photo(FSInputFile(photo["path"]))
                sent_ids.append(sent.message_id)
    for file_item in files:
        sent = await query.message.answer_document(
            FSInputFile(file_item["path"], filename=file_item["filename"])
        )
        sent_ids.append(sent.message_id)

    view_packs[token] = (query.message.chat.id, sent_ids)


async def _take(query: CallbackQuery, item: dict, db_user: dict) -> None:
    if item["status"] == STATUS_PROCESSED:
        await query.answer("Сообщение уже обработано", show_alert=True)
        return
    if item["status"] == STATUS_IN_PROGRESS:
        role = ROLES.get(item["taken_by"] or "")
        who = role["in_work"] if role else "уже взято"
        if item["taken_by"] == db_user["role"]:
            await query.answer("Вы уже взяли это сообщение")
        else:
            await query.answer(who, show_alert=True)
        return
    ok = await db.take_ig_message(item["id"], db_user["role"])
    if not ok:
        await query.answer("Не удалось взять сообщение", show_alert=True)
        return
    await query.answer("Сообщение взято в работу")
    await refresh_ig_cards(query.bot, item["id"])


async def _release(query: CallbackQuery, item: dict, db_user: dict) -> None:
    if item["status"] != STATUS_IN_PROGRESS:
        await query.answer("Сообщение не в работе", show_alert=True)
        return
    if item["taken_by"] != db_user["role"]:
        role = ROLES.get(item["taken_by"] or "")
        who = role["in_work"] if role else "уже в работе"
        await query.answer(f"{who}. Снять может только этот сотрудник.", show_alert=True)
        return
    ok = await db.release_ig_message(item["id"], db_user["role"])
    if not ok:
        await query.answer("Не удалось вернуть сообщение", show_alert=True)
        return
    await query.answer("Сообщение снова в ожидании")
    await refresh_ig_cards(query.bot, item["id"])


async def _start_reply(
    query: CallbackQuery, item: dict, state: FSMContext, db_user: dict
) -> None:
    if item["status"] == STATUS_PROCESSED:
        await query.answer("Сообщение уже обработано", show_alert=True)
        return
    current = await state.get_state()
    if current == ReplyStates.collecting:
        await query.answer("Сначала завершите текущий ответ", show_alert=True)
        return
    if item["status"] != STATUS_IN_PROGRESS:
        await db.take_ig_message(item["id"], db_user["role"])
        await refresh_ig_cards(query.bot, item["id"])
    elif item["taken_by"] and item["taken_by"] != db_user["role"]:
        role = ROLES.get(item["taken_by"])
        who = role["in_work"] if role else "уже в работе"
        await query.answer(f"{who}. Ответить может только этот сотрудник.", show_alert=True)
        return

    await state.set_state(ReplyStates.collecting)
    await query.answer()
    prompt = await query.message.answer(
        "Отправьте текст ответа в Instagram. Можно прикрепить фото или видео.\n"
        "Когда всё готово — нажмите «Отправить».",
        reply_markup=reply_keyboard(),
    )
    await state.update_data(
        source="instagram",
        ig_id=item["id"],
        text_parts=[],
        files=[],
        chat_id=query.message.chat.id,
        message_ids=[prompt.message_id],
    )


async def _delete(query: CallbackQuery, item: dict) -> None:
    await db.soft_delete_ig(item["id"])
    await query.answer("Сообщение удалено из бота", show_alert=True)
    await refresh_ig_cards(query.bot, item["id"])


async def send_ig_cards(message: Message) -> int:
    items = await db.list_open_ig_messages()
    for item in reversed(items):
        sent = await message.answer(
            format_ig_card(item),
            reply_markup=ig_keyboard(item),
            parse_mode="HTML",
        )
        await db.add_ig_notification(item["id"], message.chat.id, sent.message_id)
    return len(items)
