import shutil
from pathlib import Path

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
import db
from cards import refresh_cards, refresh_ig_cards
from ig_client import InstagramNotAuthorized, mark_ig_seen, send_ig_reply
from keyboards import ReplyCB, reply_keyboard
from mail_imap import mark_seen
from mail_smtp import send_reply
from states import ReplyStates

router = Router()


def _reply_dir(user_id: int) -> Path:
    path = config.REPLY_STORAGE / str(user_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _remember(state: FSMContext, *message_ids: int) -> None:
    data = await state.get_data()
    ids = list(data.get("message_ids") or [])
    ids.extend(message_ids)
    await state.update_data(message_ids=ids)


async def _cleanup_messages(bot, state: FSMContext, chat_id: int) -> None:
    data = await state.get_data()
    ids = list(data.get("message_ids") or [])
    for message_id in ids:
        try:
            await bot.delete_message(chat_id, message_id)
        except TelegramBadRequest:
            pass


@router.message(ReplyStates.collecting, F.text & ~F.text.startswith("/"))
async def collect_text(message: Message, state: FSMContext):
    data = await state.get_data()
    parts = data.get("text_parts") or []
    parts.append(message.text)
    await state.update_data(text_parts=parts)
    ack = await message.answer("Текст добавлен. Можно дописать или прикрепить файлы.")
    await _remember(state, message.message_id, ack.message_id)


@router.message(ReplyStates.collecting, F.photo)
async def collect_photo(message: Message, state: FSMContext, bot):
    await _save_file(message, state, bot, kind="photo")


@router.message(ReplyStates.collecting, F.document)
async def collect_document(message: Message, state: FSMContext, bot):
    await _save_file(message, state, bot, kind="document")


@router.message(ReplyStates.collecting, F.video)
async def collect_video(message: Message, state: FSMContext, bot):
    await _save_file(message, state, bot, kind="video")


@router.message(ReplyStates.collecting, F.audio)
async def collect_audio(message: Message, state: FSMContext, bot):
    await _save_file(message, state, bot, kind="audio")


async def _save_file(message: Message, state: FSMContext, bot, kind: str) -> None:
    folder = _reply_dir(message.from_user.id)
    filename = "file"
    file_id = None
    if kind == "photo":
        file_id = message.photo[-1].file_id
        filename = f"{file_id}.jpg"
    elif kind == "document":
        file_id = message.document.file_id
        filename = message.document.file_name or f"{file_id}"
    elif kind == "video":
        file_id = message.video.file_id
        filename = message.video.file_name or f"{file_id}.mp4"
    elif kind == "audio":
        file_id = message.audio.file_id
        filename = message.audio.file_name or f"{file_id}.mp3"

    dest = folder / filename
    tg_file = await bot.get_file(file_id)
    await bot.download_file(tg_file.file_path, destination=dest)

    data = await state.get_data()
    files = data.get("files") or []
    files.append({"path": str(dest), "filename": filename})
    text_parts = data.get("text_parts") or []
    if message.caption:
        text_parts.append(message.caption)
    await state.update_data(files=files, text_parts=text_parts)
    ack = await message.answer("Файл добавлен.")
    await _remember(state, message.message_id, ack.message_id)


@router.callback_query(ReplyStates.collecting, ReplyCB.filter(F.act == "cancel"))
async def cancel_reply(query: CallbackQuery, state: FSMContext):
    folder = _reply_dir(query.from_user.id)
    shutil.rmtree(folder, ignore_errors=True)
    await _cleanup_messages(query.bot, state, query.message.chat.id)
    await state.clear()
    await query.answer("Ответ отменён")


@router.callback_query(ReplyStates.collecting, ReplyCB.filter(F.act == "send"))
async def send_collected_reply(query: CallbackQuery, state: FSMContext, db_user: dict):
    data = await state.get_data()
    source = data.get("source") or "email"
    if source == "instagram":
        await _send_instagram_reply(query, state, db_user, data)
        return

    email_id = data.get("email_id")
    email = await db.get_email(email_id) if email_id else None
    if not email or email["deleted"]:
        await _cleanup_messages(query.bot, state, query.message.chat.id)
        await state.clear()
        await query.answer("Письмо не найдено", show_alert=True)
        return

    text = "\n".join(data.get("text_parts") or []).strip()
    files = data.get("files") or []
    if not text and not files:
        await query.answer("Добавьте текст или файл", show_alert=True)
        return

    to_addr = email.get("reply_to") or email.get("from_addr")
    if not to_addr:
        await query.answer("Не найден адрес для ответа", show_alert=True)
        return

    subject = email.get("subject") or ""
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}" if subject else "Re:"

    await query.answer("Отправляю ответ...")
    try:
        await send_reply(
            to_addr=to_addr,
            subject=subject,
            body=text,
            attachments=files,
            in_reply_to=email.get("message_id"),
        )
    except Exception:
        await query.message.edit_text(
            "Не удалось отправить письмо. Проверьте MAIL_PASSWORD и доступ к SMTP.",
            reply_markup=reply_keyboard(),
        )
        return

    await db.mark_processed(email["id"], db_user["role"])
    await mark_seen(email.get("imap_uid"), email.get("imap_folder"))
    await refresh_cards(query.bot, email["id"])
    shutil.rmtree(_reply_dir(query.from_user.id), ignore_errors=True)
    await _cleanup_messages(query.bot, state, query.message.chat.id)
    await state.clear()


async def _send_instagram_reply(
    query: CallbackQuery, state: FSMContext, db_user: dict, data: dict
) -> None:
    ig_id = data.get("ig_id")
    item = await db.get_ig_message(ig_id) if ig_id else None
    if not item or item["deleted"]:
        await _cleanup_messages(query.bot, state, query.message.chat.id)
        await state.clear()
        await query.answer("Сообщение не найдено", show_alert=True)
        return

    text = "\n".join(data.get("text_parts") or []).strip()
    files = data.get("files") or []
    if not text and not files:
        await query.answer("Добавьте текст или файл", show_alert=True)
        return

    thread_id = item.get("thread_id")
    if not thread_id:
        await query.answer("Не найден диалог Instagram для ответа", show_alert=True)
        return

    await query.answer("Отправляю ответ в Instagram...")
    try:
        await send_ig_reply(
            thread_id=thread_id,
            text=text,
            files=files,
            is_pending=bool(item.get("is_pending")),
        )
    except InstagramNotAuthorized:
        await query.message.edit_text(
            "Instagram не авторизован. Администратор должен выполнить /ig_login.",
            reply_markup=reply_keyboard(),
        )
        return
    except Exception:
        await query.message.edit_text(
            "Не удалось отправить сообщение в Instagram.",
            reply_markup=reply_keyboard(),
        )
        return

    await db.mark_ig_processed(item["id"], db_user["role"])
    await mark_ig_seen(thread_id, item.get("ig_id"))
    await refresh_ig_cards(query.bot, item["id"])
    shutil.rmtree(_reply_dir(query.from_user.id), ignore_errors=True)
    await _cleanup_messages(query.bot, state, query.message.chat.id)
    await state.clear()


@router.message(ReplyStates.collecting, ~F.text.startswith("/"))
async def collect_other(message: Message, state: FSMContext):
    ack = await message.answer(
        "Отправьте текст или файл, либо нажмите «Отправить» / «Отмена»."
    )
    await _remember(state, message.message_id, ack.message_id)
