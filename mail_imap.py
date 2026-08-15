import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from imap_tools import AND, MailBox, MailMessageFlags

import config
import db
from keyboards import email_keyboard
from mail_parser import extract_body, extract_reply_to, parse_from
from utils import format_card

logger = logging.getLogger(__name__)


def _message_id(msg) -> str:
    headers = msg.headers or {}
    raw = ""
    for key, val in headers.items():
        if str(key).lower() == "message-id":
            raw = val[0] if isinstance(val, (list, tuple)) else str(val)
            break
    return (raw or "").strip() or str(msg.uid)


def _safe_filename(name: str) -> str:
    name = Path(name or "file").name
    name = re.sub(r'[<>:"/\\|?*]+', "_", name).strip()
    return name or "file"


def _fetch_new_sync(last_uid: int) -> tuple[list[dict], int]:
    messages: list[dict] = []
    max_uid = last_uid

    with MailBox(config.IMAP_HOST, port=config.IMAP_PORT).login(
        config.MAIL_USERNAME,
        config.MAIL_PASSWORD,
    ) as mailbox:
        folder_max = last_uid
        if last_uid <= 0:
            uids = mailbox.uids()
            folder_max = max((int(u) for u in uids), default=0)
            criteria = AND(seen=False)
        else:
            criteria = f"UID {last_uid + 1}:*"

        for msg in mailbox.fetch(criteria, mark_seen=False, bulk=True):
            uid = int(msg.uid)
            if last_uid > 0 and uid <= last_uid:
                continue
            max_uid = max(max_uid, uid)

            from_addr = parse_from(msg.from_ or "")
            if from_addr.lower() == config.MAIL_USERNAME.lower():
                continue

            body = extract_body(msg.text, msg.html)
            attachments = []
            for att in msg.attachments:
                mime = (att.content_type or "").lower()
                attachments.append(
                    {
                        "filename": att.filename or "file",
                        "payload": att.payload or b"",
                        "mime": mime,
                        "is_image": mime.startswith("image/"),
                    }
                )

            received = msg.date.isoformat() if msg.date else datetime.now().isoformat()
            messages.append(
                {
                    "imap_uid": str(uid),
                    "message_id": _message_id(msg),
                    "subject": msg.subject or "",
                    "from_addr": from_addr,
                    "body": body,
                    "reply_to": extract_reply_to(from_addr, body),
                    "received_at": received,
                    "attachments": attachments,
                }
            )

        max_uid = max(max_uid, folder_max if last_uid <= 0 else max_uid)

    return messages, max_uid


async def _save_and_notify(bot: Bot, item: dict) -> None:
    message_id = (item["message_id"] or "").strip() or item["imap_uid"]
    if await db.email_exists(message_id):
        return

    email_id = await db.insert_email(
        imap_uid=item["imap_uid"],
        message_id=message_id,
        subject=item["subject"],
        from_addr=item["from_addr"],
        reply_to=item["reply_to"],
        body=item["body"],
        received_at=item["received_at"],
    )

    folder = config.MAIL_STORAGE / str(email_id)
    folder.mkdir(parents=True, exist_ok=True)
    for index, att in enumerate(item["attachments"], start=1):
        payload = att["payload"]
        if not payload:
            continue
        filename = _safe_filename(att["filename"])
        if filename == "file":
            filename = f"file_{index}"
        path = folder / filename
        if path.exists():
            path = folder / f"{index}_{filename}"
        path.write_bytes(payload)
        await db.add_attachment(
            email_id, filename, str(path), att["mime"], att["is_image"]
        )

    email = await db.get_email(email_id)
    if not email:
        return

    text = format_card(email, is_new=True)
    markup = email_keyboard(email)
    for user in await db.list_active_users():
        try:
            sent = await bot.send_message(
                user["telegram_id"],
                text,
                reply_markup=markup,
                parse_mode="HTML",
            )
            await db.add_notification(email_id, user["telegram_id"], sent.message_id)
        except TelegramForbiddenError:
            logger.warning("Пользователь %s заблокировал бота", user["telegram_id"])
        except Exception:
            logger.exception("Не удалось отправить карточку письма")


async def process_new_mail(bot: Bot) -> None:
    last_uid_raw = await db.get_setting("last_uid")
    last_uid = int(last_uid_raw) if last_uid_raw else 0
    loop = asyncio.get_running_loop()
    messages, max_uid = await loop.run_in_executor(None, _fetch_new_sync, last_uid)

    for item in messages:
        try:
            await _save_and_notify(bot, item)
        except Exception:
            logger.exception("Ошибка сохранения письма UID=%s", item.get("imap_uid"))

    if messages:
        logger.info("Обработано новых писем: %s", len(messages))

    if max_uid > last_uid:
        await db.set_setting("last_uid", str(max_uid))


def _mark_seen_sync(imap_uid: str) -> None:
    if not imap_uid:
        return
    with MailBox(config.IMAP_HOST, port=config.IMAP_PORT).login(
        config.MAIL_USERNAME,
        config.MAIL_PASSWORD,
    ) as mailbox:
        mailbox.flag(imap_uid, MailMessageFlags.SEEN, True)


async def mark_seen(imap_uid: str | None) -> None:
    if not imap_uid or not config.MAIL_PASSWORD:
        return
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _mark_seen_sync, str(imap_uid))
        logger.info("Письмо UID=%s отмечено как прочитанное", imap_uid)
    except Exception:
        logger.exception("Не удалось отметить письмо UID=%s как прочитанное", imap_uid)


async def mail_loop(bot: Bot) -> None:
    config.MAIL_STORAGE.mkdir(parents=True, exist_ok=True)
    if not config.MAIL_PASSWORD:
        logger.warning("MAIL_PASSWORD не задан — проверка почты отключена")
        return
    while True:
        try:
            await process_new_mail(bot)
        except Exception:
            logger.exception("Ошибка цикла проверки почты")
        await asyncio.sleep(config.MAIL_POLL_INTERVAL)
