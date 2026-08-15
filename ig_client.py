import asyncio
import logging
import mimetypes
import re
import threading
from datetime import datetime
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError

import config
import db
from keyboards import ig_keyboard
from utils import format_ig_card

logger = logging.getLogger(__name__)

SKIP_TYPES = {"action_log", "video_call_event"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v"}
VOICE_EXTS = {".m4a"}

_client = None
_client_lock = threading.Lock()
_async_lock = asyncio.Lock()

_pending_kind: str | None = None
_code_event = threading.Event()
_code_value: str | None = None


def pending_code_kind() -> str | None:
    return _pending_kind


def submit_code(code: str) -> bool:
    global _code_value
    value = (code or "").strip()
    if not value:
        return False
    _code_value = value
    _code_event.set()
    return True


def _request_code(kind: str) -> str | None:
    global _pending_kind, _code_value
    _pending_kind = kind
    _code_value = None
    _code_event.clear()
    logger.warning("Instagram запросил код подтверждения (%s)", kind)
    if not _code_event.wait(timeout=300):
        _pending_kind = None
        return None
    _pending_kind = None
    return _code_value


def _challenge_code_handler(username, choice) -> str | bool:
    try:
        from instagrapi.mixins.challenge import ChallengeChoice

        kind = "sms" if choice == ChallengeChoice.SMS else "email"
    except Exception:
        kind = "email"
    code = _request_code(kind)
    return code or False


def _safe_filename(name: str) -> str:
    name = Path(name or "file").name
    name = re.sub(r'[<>:"/\\|?*]+', "_", name).strip()
    return name or "file"


def _as_url(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _as_thread_id(value) -> int | str:
    text = str(value)
    return int(text) if text.isdigit() else text


def _guess_mime(filename: str, fallback: str = "application/octet-stream") -> str:
    mime, _ = mimetypes.guess_type(filename)
    return mime or fallback


def _is_own(msg, own_id: str) -> bool:
    if getattr(msg, "is_sent_by_viewer", False):
        return True
    user_id = getattr(msg, "user_id", None)
    return user_id is not None and str(user_id) == own_id


def _user_map(thread) -> dict[str, object]:
    result = {}
    for user in getattr(thread, "users", None) or []:
        pk = getattr(user, "pk", None)
        if pk is not None:
            result[str(pk)] = user
    return result


def _sender_info(msg, users: dict) -> tuple[str, str, str]:
    user_id = str(getattr(msg, "user_id", "") or "")
    user = users.get(user_id)
    username = getattr(user, "username", "") if user else ""
    name = getattr(user, "full_name", "") if user else ""
    return user_id, username or "", name or ""


def _dict_text(value) -> str:
    if not value:
        return ""
    if isinstance(value, dict):
        for key in ("text", "message", "title", "caption"):
            item = value.get(key)
            if item:
                return str(item)
    text = getattr(value, "text", None)
    if text:
        return str(text)
    return ""


def _message_body(msg) -> str:
    parts: list[str] = []
    if msg.text:
        parts.append(msg.text)
    link = getattr(msg, "link", None)
    if link:
        if getattr(link, "text", None):
            parts.append(link.text)
        context = getattr(link, "link_context", None)
        url = getattr(context, "link_url", None) if context else None
        if url:
            parts.append(str(url))
    media_share = getattr(msg, "media_share", None)
    if media_share:
        code = getattr(media_share, "code", None)
        caption = getattr(media_share, "caption_text", None) or ""
        url = f"https://www.instagram.com/p/{code}/" if code else ""
        line = "Поделился публикацией"
        if url:
            line += f": {url}"
        if caption:
            line += f"\n{caption}"
        parts.append(line)
    clip = getattr(msg, "clip", None)
    if clip:
        code = getattr(clip, "code", None)
        caption = getattr(clip, "caption_text", None) or ""
        url = f"https://www.instagram.com/reel/{code}/" if code else ""
        line = "Поделился Reels"
        if url:
            line += f": {url}"
        if caption:
            line += f"\n{caption}"
        parts.append(line)
    story = _dict_text(getattr(msg, "story_share", None))
    if story:
        parts.append(f"Ответ на сторис: {story}" if story else "Ответ на сторис")
    elif getattr(msg, "story_share", None):
        parts.append("Ответ на сторис")
    reel = _dict_text(getattr(msg, "reel_share", None))
    if reel:
        parts.append(reel)
    xma = getattr(msg, "xma_share", None)
    if xma:
        title = getattr(xma, "title", None) or getattr(xma, "header_title_text", None)
        url = getattr(xma, "video_url", None)
        if title:
            parts.append(str(title))
        if url:
            parts.append(str(url))
    placeholder = getattr(msg, "placeholder", None)
    if placeholder:
        text = _dict_text(placeholder) or str(placeholder)
        if text:
            parts.append(text)
    item_type = getattr(msg, "item_type", None) or ""
    if not parts:
        labels = {
            "media": "Фото или видео",
            "raven_media": "Исчезающее фото/видео",
            "voice_media": "Голосовое сообщение",
            "animated_media": "GIF",
            "clip": "Reels",
            "media_share": "Публикация",
        }
        parts.append(labels.get(item_type, "Сообщение Instagram"))
    return "\n".join(part for part in parts if part).strip()


def _download_bytes(client, url: str) -> bytes | None:
    if not url:
        return None
    try:
        response = client.private.get(str(url), timeout=30)
        response.raise_for_status()
        return response.content or None
    except Exception:
        logger.exception("Не удалось скачать вложение Instagram")
        return None


def _add_attachment(items: list[dict], payload: bytes | None, filename: str, mime: str) -> None:
    if not payload:
        return
    items.append(
        {
            "filename": filename,
            "payload": payload,
            "mime": mime,
            "is_image": mime.startswith("image/"),
        }
    )


def _collect_attachments(client, msg) -> list[dict]:
    attachments: list[dict] = []
    media = getattr(msg, "media", None)
    if media:
        video_url = _as_url(getattr(media, "video_url", None))
        audio_url = _as_url(getattr(media, "audio_url", None))
        photo_url = _as_url(getattr(media, "thumbnail_url", None))
        if video_url:
            _add_attachment(
                attachments,
                _download_bytes(client, video_url),
                "video.mp4",
                "video/mp4",
            )
        elif audio_url:
            _add_attachment(
                attachments,
                _download_bytes(client, audio_url),
                "voice.m4a",
                "audio/mp4",
            )
        elif photo_url:
            _add_attachment(
                attachments,
                _download_bytes(client, photo_url),
                "photo.jpg",
                "image/jpeg",
            )

    visual = getattr(msg, "visual_media", None)
    visual_media = getattr(visual, "media", None) if visual else None
    if visual_media:
        videos = getattr(visual_media, "video_versions", None) or []
        images = getattr(getattr(visual_media, "image_versions2", None), "candidates", None) or []
        if videos:
            url = _as_url(getattr(videos[-1], "url", None))
            _add_attachment(
                attachments,
                _download_bytes(client, url),
                "snap.mp4",
                "video/mp4",
            )
        elif images:
            url = _as_url(getattr(images[-1], "url", None))
            _add_attachment(
                attachments,
                _download_bytes(client, url),
                "snap.jpg",
                "image/jpeg",
            )

    for attr, filename, mime in (
        ("clip", "reel.mp4", "video/mp4"),
        ("media_share", "share.jpg", "image/jpeg"),
    ):
        shared = getattr(msg, attr, None)
        if not shared:
            continue
        video_url = _as_url(getattr(shared, "video_url", None))
        photo_url = _as_url(getattr(shared, "thumbnail_url", None))
        if video_url:
            _add_attachment(attachments, _download_bytes(client, video_url), filename, mime)
        elif photo_url:
            _add_attachment(
                attachments,
                _download_bytes(client, photo_url),
                "share.jpg",
                "image/jpeg",
            )

    xma = getattr(msg, "xma_share", None)
    preview = _as_url(getattr(xma, "preview_url", None) if xma else None)
    if preview.startswith("http"):
        mime = getattr(xma, "preview_url_mime_type", None) or "image/jpeg"
        ext = ".jpg" if "jpeg" in mime or "jpg" in mime else ".bin"
        _add_attachment(
            attachments,
            _download_bytes(client, preview),
            f"preview{ext}",
            mime,
        )

    animated = getattr(msg, "animated_media", None)
    if isinstance(animated, dict):
        images = (animated.get("images") or {}) if isinstance(animated.get("images"), dict) else {}
        url = ""
        for key in ("fixed_height", "original", "downsized"):
            item = images.get(key) or {}
            if isinstance(item, dict) and item.get("url"):
                url = item["url"]
                break
        if url:
            _add_attachment(
                attachments,
                _download_bytes(client, url),
                "gif.gif",
                "image/gif",
            )

    voice = getattr(msg, "voice_media", None)
    if voice:
        audio = getattr(voice, "audio", None) or voice
        url = _as_url(
            getattr(audio, "audio_src", None)
            or getattr(audio, "url", None)
            or (audio.get("audio_src") if isinstance(audio, dict) else None)
        )
        if url:
            _add_attachment(
                attachments,
                _download_bytes(client, url),
                "voice.m4a",
                "audio/mp4",
            )
    return attachments


def _new_client():
    from instagrapi import Client

    client = Client()
    client.delay_range = [1, 3]
    client.challenge_code_handler = _challenge_code_handler
    return client


def _login_sync():
    from instagrapi.exceptions import ChallengeRequired, LoginRequired, TwoFactorRequired

    global _client
    session_file = config.IG_SESSION_FILE
    session_file.parent.mkdir(parents=True, exist_ok=True)

    if session_file.exists():
        client = _new_client()
        try:
            client.load_settings(session_file)
            client.login(config.IG_USERNAME, config.IG_PASSWORD)
            client.dump_settings(session_file)
            logger.info("Вход в Instagram выполнен по сохранённой сессии")
            _client = client
            return client
        except LoginRequired:
            logger.warning("Сессия Instagram истекла, выполняю повторный вход")
        except TwoFactorRequired:
            code = _request_code("2fa")
            if not code:
                raise RuntimeError("Не получен код двухфакторной аутентификации Instagram")
            client.login(config.IG_USERNAME, config.IG_PASSWORD, verification_code=code)
            client.dump_settings(session_file)
            logger.info("Вход в Instagram выполнен после 2FA")
            _client = client
            return client
        except ChallengeRequired:
            logger.exception("Instagram запросил ручную проверку аккаунта")
            raise
        except Exception:
            logger.exception("Не удалось использовать сохранённую сессию Instagram")

    client = _new_client()
    try:
        client.login(config.IG_USERNAME, config.IG_PASSWORD)
    except TwoFactorRequired:
        code = _request_code("2fa")
        if not code:
            raise RuntimeError("Не получен код двухфакторной аутентификации Instagram")
        client.login(config.IG_USERNAME, config.IG_PASSWORD, verification_code=code)
    client.dump_settings(session_file)
    logger.info("Вход в Instagram выполнен")
    _client = client
    return client


def _get_client():
    global _client
    with _client_lock:
        if _client is None:
            return _login_sync()
        return _client


def _reset_client() -> None:
    global _client
    with _client_lock:
        _client = None


def _fetch_threads(client, pending: bool) -> list:
    if pending:
        for method_name in ("direct_pending_inbox", "direct_requests"):
            method = getattr(client, method_name, None)
            if method is None:
                continue
            try:
                return method(amount=20) or []
            except Exception:
                logger.exception("Не удалось получить запросы Instagram (%s)", method_name)
        return []

    try:
        return (
            client.direct_threads(
                amount=20,
                selected_filter="unread",
                thread_message_limit=10,
            )
            or []
        )
    except TypeError:
        try:
            return client.direct_threads(amount=20, selected_filter="unread") or []
        except TypeError:
            threads = client.direct_threads(amount=20) or []
    except Exception:
        logger.exception("Не удалось получить непрочитанные диалоги Instagram")
        try:
            threads = client.direct_threads(amount=20) or []
        except Exception:
            logger.exception("Не удалось получить входящие Instagram")
            return []
    own_id = str(client.user_id)
    unread = []
    for thread in threads:
        try:
            if not thread.is_seen(own_id):
                unread.append(thread)
        except Exception:
            unread.append(thread)
    return unread


def _parse_thread(client, thread, pending: bool) -> list[dict]:
    own_id = str(client.user_id)
    users = _user_map(thread)
    items: list[dict] = []
    messages = list(getattr(thread, "messages", None) or [])
    incoming = []
    for msg in messages:
        item_type = getattr(msg, "item_type", None) or ""
        if item_type in SKIP_TYPES:
            continue
        if _is_own(msg, own_id):
            continue
        incoming.append(msg)
        if len(incoming) >= 5:
            break
    incoming.reverse()
    thread_id = str(getattr(thread, "id", None) or getattr(thread, "pk", "") or "")
    for msg in incoming:
        ig_id = str(getattr(msg, "id", "") or "")
        if not ig_id:
            continue
        sender_id, username, name = _sender_info(msg, users)
        received = msg.timestamp.isoformat() if getattr(msg, "timestamp", None) else datetime.now().isoformat()
        items.append(
            {
                "ig_id": ig_id,
                "thread_id": thread_id,
                "sender_id": sender_id,
                "sender_username": username,
                "sender_name": name,
                "body": _message_body(msg),
                "received_at": received,
                "is_pending": pending,
                "attachments": _collect_attachments(client, msg),
            }
        )
    return items


def _fetch_new_sync() -> list[dict]:
    from instagrapi.exceptions import LoginRequired, PleaseWaitFewMinutes

    try:
        client = _get_client()
        threads = _fetch_threads(client, pending=False)
        pending_threads = _fetch_threads(client, pending=True)
        messages: list[dict] = []
        seen_ids: set[str] = set()
        for pending, group in ((False, threads), (True, pending_threads)):
            for thread in group:
                for item in _parse_thread(client, thread, pending):
                    if item["ig_id"] in seen_ids:
                        continue
                    seen_ids.add(item["ig_id"])
                    messages.append(item)
        try:
            client.dump_settings(config.IG_SESSION_FILE)
        except Exception:
            logger.debug("Не удалось сохранить сессию Instagram", exc_info=True)
        return messages
    except PleaseWaitFewMinutes:
        logger.warning("Instagram просит подождать, пропускаю этот цикл")
        return []
    except LoginRequired:
        logger.warning("Сессия Instagram недействительна, пробую войти заново")
        _reset_client()
        client = _get_client()
        threads = _fetch_threads(client, pending=False)
        pending_threads = _fetch_threads(client, pending=True)
        messages = []
        seen_ids: set[str] = set()
        for pending, group in ((False, threads), (True, pending_threads)):
            for thread in group:
                for item in _parse_thread(client, thread, pending):
                    if item["ig_id"] in seen_ids:
                        continue
                    seen_ids.add(item["ig_id"])
                    messages.append(item)
        return messages


def _send_reply_sync(thread_id: str, text: str, files: list[dict], is_pending: bool) -> None:
    from instagrapi.exceptions import LoginRequired

    client = _get_client()
    tid = _as_thread_id(thread_id)
    if is_pending:
        approve = getattr(client, "direct_request_approve", None) or getattr(
            client, "direct_pending_approve", None
        )
        if approve:
            try:
                approve(tid)
            except Exception:
                logger.exception("Не удалось принять запрос в сообщениях Instagram")
    skipped: list[str] = []
    if text:
        client.direct_answer(tid, text)
    for item in files:
        path = Path(item["path"])
        if not path.exists():
            continue
        suffix = path.suffix.lower()
        try:
            if suffix in IMAGE_EXTS:
                client.direct_send_photo(path, thread_ids=[tid])
            elif suffix in VIDEO_EXTS:
                client.direct_send_video(path, thread_ids=[tid])
            elif suffix in VOICE_EXTS:
                client.direct_send_voice(path, thread_ids=[tid])
            else:
                skipped.append(item.get("filename") or path.name)
        except LoginRequired:
            _reset_client()
            raise
        except Exception:
            logger.exception("Не удалось отправить файл в Instagram: %s", path.name)
            skipped.append(item.get("filename") or path.name)
    if skipped:
        note = "Не удалось отправить файлы: " + ", ".join(skipped)
        if not text:
            client.direct_answer(tid, note)
        else:
            logger.warning(note)
    _mark_seen_sync(thread_id)


def _mark_seen_sync(thread_id: str, message_id: str | None = None) -> None:
    client = _get_client()
    tid = _as_thread_id(thread_id)
    if message_id and hasattr(client, "direct_message_seen"):
        try:
            client.direct_message_seen(tid, _as_thread_id(message_id))
            return
        except Exception:
            logger.debug("Не удалось отметить сообщение Instagram как прочитанное", exc_info=True)
    if hasattr(client, "direct_send_seen"):
        client.direct_send_seen(tid)


async def send_ig_reply(
    *,
    thread_id: str,
    text: str,
    files: list[dict],
    is_pending: bool = False,
) -> None:
    async with _async_lock:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, _send_reply_sync, thread_id, text, files, is_pending
        )


async def mark_ig_seen(thread_id: str | None, ig_id: str | None = None) -> None:
    if not thread_id:
        return
    async with _async_lock:
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, _mark_seen_sync, thread_id, ig_id)
            logger.info("Диалог Instagram %s отмечен как прочитанный", thread_id)
        except Exception:
            logger.exception(
                "Не удалось отметить диалог Instagram %s как прочитанный", thread_id
            )


async def _save_and_notify(bot: Bot, item: dict) -> None:
    if await db.ig_message_exists(item["ig_id"]):
        return
    message_id = await db.insert_ig_message(
        ig_id=item["ig_id"],
        thread_id=item["thread_id"],
        sender_id=item["sender_id"],
        sender_username=item["sender_username"],
        sender_name=item["sender_name"],
        body=item["body"],
        received_at=item["received_at"],
        is_pending=bool(item.get("is_pending")),
    )
    folder = config.IG_STORAGE / str(message_id)
    folder.mkdir(parents=True, exist_ok=True)
    for index, att in enumerate(item.get("attachments") or [], start=1):
        payload = att.get("payload")
        if not payload:
            continue
        filename = _safe_filename(att.get("filename") or f"file_{index}")
        path = folder / filename
        if path.exists():
            path = folder / f"{index}_{filename}"
        path.write_bytes(payload)
        await db.add_ig_attachment(
            message_id, filename, str(path), att.get("mime") or _guess_mime(filename), att.get("is_image", False)
        )

    saved = await db.get_ig_message(message_id)
    if not saved:
        return
    text = format_ig_card(saved, is_new=True)
    markup = ig_keyboard(saved)
    for user in await db.list_active_users():
        try:
            sent = await bot.send_message(
                user["telegram_id"],
                text,
                reply_markup=markup,
                parse_mode="HTML",
            )
            await db.add_ig_notification(message_id, user["telegram_id"], sent.message_id)
        except TelegramForbiddenError:
            logger.warning("Пользователь %s заблокировал бота", user["telegram_id"])
        except Exception:
            logger.exception("Не удалось отправить карточку Instagram")


async def process_new_ig(bot: Bot) -> None:
    async with _async_lock:
        loop = asyncio.get_running_loop()
        messages = await loop.run_in_executor(None, _fetch_new_sync)
    for item in messages:
        try:
            await _save_and_notify(bot, item)
        except Exception:
            logger.exception("Ошибка сохранения сообщения Instagram %s", item.get("ig_id"))
    if messages:
        logger.info("Обработано новых сообщений Instagram: %s", len(messages))


async def _notify_code_watch(bot: Bot) -> None:
    last = None
    labels = {
        "sms": "из SMS",
        "email": "из письма Instagram",
        "2fa": "из приложения двухфакторной аутентификации",
    }
    while True:
        kind = pending_code_kind()
        if kind and kind != last:
            where = labels.get(kind, kind)
            text = (
                f"Instagram запросил код подтверждения ({where}).\n"
                "Отправьте его командой:\n"
                "/ig_code 123456"
            )
            for user in await db.list_active_users():
                try:
                    await bot.send_message(user["telegram_id"], text)
                except Exception:
                    logger.exception("Не удалось отправить запрос кода Instagram")
        last = kind
        await asyncio.sleep(1)


async def ig_loop(bot: Bot) -> None:
    config.IG_STORAGE.mkdir(parents=True, exist_ok=True)
    if not config.IG_USERNAME or not config.IG_PASSWORD:
        logger.warning("IG_USERNAME или IG_PASSWORD не заданы — проверка Instagram отключена")
        return
    asyncio.create_task(_notify_code_watch(bot))
    while True:
        try:
            await process_new_ig(bot)
        except Exception:
            logger.exception("Ошибка цикла проверки Instagram")
        await asyncio.sleep(config.IG_POLL_INTERVAL)
