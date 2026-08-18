import asyncio
import logging
import mimetypes
import re
import threading
import time
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

_backoff_sec = 0
_backoff_until = 0.0
_MIN_BACKOFF = 10 * 60
_MAX_BACKOFF = 30 * 60

_notify_bot: Bot | None = None
_notify_loop: asyncio.AbstractEventLoop | None = None
_code_notice_sent = False
_login_busy = False
_auth_error: str | None = None
_logged_in_via: str | None = None
_session_expired_notified = False

CODE_LABELS = {
    "sms": "из SMS",
    "email": "из письма Instagram",
    "2fa": "из приложения двухфакторной аутентификации",
}


class InstagramRateLimited(Exception):
    """Instagram вернул 429 — нужно подождать."""


class InstagramNotAuthorized(Exception):
    """Нет рабочей сессии Instagram, нужен /ig_login администратора."""


def is_ig_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id == config.IG_ADMIN_ID


def is_authorized() -> bool:
    with _client_lock:
        return _client is not None


def _is_rate_limited(exc: BaseException | None) -> bool:
    from instagrapi.exceptions import ClientThrottledError, PleaseWaitFewMinutes

    types: tuple[type[BaseException], ...] = (ClientThrottledError, PleaseWaitFewMinutes)
    try:
        from instagrapi.exceptions import RateLimitError

        types += (RateLimitError,)
    except ImportError:
        pass
    try:
        from requests.exceptions import RetryError

        types += (RetryError,)
    except ImportError:
        pass

    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, types):
            return True
        text = str(current).lower()
        if "429" in text or "too many" in text or "please wait" in text:
            return True
        current = current.__cause__ or current.__context__
    return False


def _format_auth_error(exc: BaseException) -> str:
    if isinstance(exc, InstagramRateLimited) or _is_rate_limited(exc):
        return "слишком много попыток входа (429)"
    text = str(exc).strip() or type(exc).__name__
    return text[:300]


def _mark_rate_limited() -> None:
    global _backoff_sec, _backoff_until
    _backoff_sec = min(max(_backoff_sec * 2, _MIN_BACKOFF), _MAX_BACKOFF)
    _backoff_until = time.monotonic() + _backoff_sec
    logger.warning(
        "Instagram ограничил частоту запросов (429). Пауза %s мин.",
        max(_backoff_sec, 60) // 60,
    )


def _reset_backoff() -> None:
    global _backoff_sec, _backoff_until
    was_limited = _backoff_sec > 0
    _backoff_sec = 0
    _backoff_until = 0.0
    if was_limited:
        logger.info("Лимит Instagram снят, проверка продолжается")


def _notify_admin_sync(text: str) -> bool:
    bot = _notify_bot
    loop = _notify_loop
    if not bot or not loop:
        logger.warning("Telegram-бот ещё не готов отправить уведомление администратору")
        return False
    try:
        fut = asyncio.run_coroutine_threadsafe(_send_to_admin(bot, text), loop)
        fut.result(timeout=30)
        return True
    except Exception:
        logger.exception("Не удалось отправить уведомление администратору Instagram")
        return False


async def _send_to_admin(bot: Bot, text: str) -> None:
    try:
        await bot.send_message(config.IG_ADMIN_ID, text)
    except Exception:
        logger.exception("Не удалось написать администратору Instagram (%s)", config.IG_ADMIN_ID)


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
    global _pending_kind, _code_value, _code_notice_sent
    _pending_kind = kind
    _code_value = None
    _code_notice_sent = False
    _code_event.clear()
    logger.warning("Instagram запросил код подтверждения (%s)", kind)
    where = CODE_LABELS.get(kind, kind)
    _code_notice_sent = _notify_admin_sync(
        f"Instagram запросил код подтверждения ({where}).\n"
        "Отправьте его командой:\n"
        "/ig_code 123456"
    )
    if not _code_event.wait(timeout=900):
        _pending_kind = None
        logger.warning("Код подтверждения Instagram не получен за 15 минут")
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
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    try:
        client = Client(
            delay_range=[2, 5],
            session_retry_total=0,
            session_retry_statuses=[500, 502, 503, 504],
        )
    except TypeError:
        client = Client()
        client.delay_range = [2, 5]
    client.challenge_code_handler = _challenge_code_handler
    try:
        retry = Retry(total=0, raise_on_status=False)
        adapter = HTTPAdapter(max_retries=retry)
        for session in (getattr(client, "private", None), getattr(client, "public", None)):
            if session is None:
                continue
            session.mount("https://", adapter)
            session.mount("http://", adapter)
    except Exception:
        logger.debug("Не удалось отключить повторы HTTP 429", exc_info=True)
    return client


def _apply_fresh_device(client) -> None:
    """Новый отпечаток устройства для ручного входа. Сохранённую сессию не трогаем."""
    current = dict(getattr(client, "device_settings", None) or {})
    device = {
        "app_version": current.get("app_version") or "269.0.0.18.75",
        "android_version": 34,
        "android_release": "14",
        "dpi": "480dpi",
        "resolution": "1080x2340",
        "manufacturer": "samsung",
        "device": "a54x",
        "model": "SM-A546B",
        "cpu": "s5e8835",
        "version_code": current.get("version_code") or "314665256",
    }
    try:
        client.set_device(device, reset=True)
    except TypeError:
        client.set_device(device)
        client.set_uuids({})
    try:
        client.set_user_agent()
    except Exception:
        logger.debug("Не удалось обновить User-Agent Instagram", exc_info=True)
    try:
        client.set_locale("ru_RU")
        client.set_country("BY")
        client.set_country_code(375)
        client.set_timezone_offset(3 * 3600)
    except Exception:
        logger.debug("Не удалось задать локаль Instagram", exc_info=True)
    logger.info(
        "Устройство для входа Instagram: %s %s, locale=ru_RU, country=BY",
        device["manufacturer"],
        device["model"],
    )


def _password_login(client):
    from instagrapi.exceptions import ChallengeRequired, TwoFactorRequired

    try:
        client.login(config.IG_USERNAME, config.IG_PASSWORD)
    except TwoFactorRequired:
        code = _request_code("2fa")
        if not code:
            raise RuntimeError("Не получен код двухфакторной аутентификации Instagram")
        try:
            client.login(config.IG_USERNAME, config.IG_PASSWORD, verification_code=code)
        except Exception as e:
            if _is_rate_limited(e):
                raise InstagramRateLimited from e
            raise
        logger.info("Вход в Instagram выполнен после 2FA")
        return client
    except ChallengeRequired:
        logger.warning("Instagram запросил ручную проверку аккаунта")
        _notify_admin_sync(
            "Instagram запросил проверку аккаунта в приложении.\n"
            "Откройте Instagram на телефоне и подтвердите вход.\n"
            "Если придёт код — отправьте /ig_code 123456, затем снова /ig_login"
        )
        raise
    except Exception as e:
        if _is_rate_limited(e):
            raise InstagramRateLimited from e
        raise
    logger.info("Вход в Instagram выполнен")
    return client


def _restore_session_sync() -> bool:
    from instagrapi.exceptions import LoginRequired

    global _client, _auth_error, _logged_in_via
    with _client_lock:
        if _client is not None:
            return True
        session_file = config.IG_SESSION_FILE
        if not session_file.exists():
            _auth_error = "нет сохранённой сессии"
            return False
        client = _new_client()
        try:
            client.load_settings(session_file)
            client.get_timeline_feed()
            client.dump_settings(session_file)
            _client = client
            _logged_in_via = "session"
            _auth_error = None
            logger.info("Вход в Instagram выполнен по сохранённой сессии")
            return True
        except LoginRequired:
            _auth_error = "сессия истекла"
            logger.warning("Сессия Instagram истекла")
            return False
        except Exception as e:
            if _is_rate_limited(e):
                _auth_error = "слишком много попыток (429) при проверке сессии"
                raise InstagramRateLimited from e
            _auth_error = _format_auth_error(e)
            logger.warning("Не удалось проверить сессию Instagram: %s", e)
            return False


def _login_by_password_sync() -> None:
    global _client, _auth_error, _logged_in_via, _session_expired_notified

    session_file = config.IG_SESSION_FILE
    session_file.parent.mkdir(parents=True, exist_ok=True)
    client = _new_client()
    _apply_fresh_device(client)
    try:
        _password_login(client)
        client.dump_settings(session_file)
    except Exception as e:
        _auth_error = _format_auth_error(e)
        raise
    with _client_lock:
        _client = client
        _logged_in_via = "password"
        _auth_error = None
        _session_expired_notified = False


def _normalize_sessionid(raw: str) -> str:
    value = (raw or "").strip().strip('"').strip("'")
    if value.lower().startswith("sessionid="):
        value = value.split("=", 1)[1].strip()
    return value


def _login_by_sessionid_sync(sessionid: str) -> None:
    global _client, _auth_error, _logged_in_via, _session_expired_notified

    sessionid = _normalize_sessionid(sessionid)
    if not sessionid:
        raise RuntimeError("Пустой sessionid")

    session_file = config.IG_SESSION_FILE
    session_file.parent.mkdir(parents=True, exist_ok=True)
    client = _new_client()
    try:
        client.set_locale("ru_RU")
        client.set_country("BY")
        client.set_country_code(375)
        client.set_timezone_offset(3 * 3600)
    except Exception:
        logger.debug("Не удалось задать локаль Instagram", exc_info=True)
    try:
        login = getattr(client, "login_by_sessionid", None)
        if login is None:
            raise RuntimeError("Эта версия instagrapi не поддерживает вход по sessionid")
        login(sessionid)
        client.dump_settings(session_file)
    except Exception as e:
        _auth_error = _format_auth_error(e)
        if _is_rate_limited(e):
            raise InstagramRateLimited from e
        raise
    logger.info("Вход в Instagram выполнен по sessionid")
    with _client_lock:
        _client = client
        _logged_in_via = "session"
        _auth_error = None
        _session_expired_notified = False


def _get_client():
    with _client_lock:
        if _client is not None:
            return _client
    if _restore_session_sync():
        with _client_lock:
            if _client is not None:
                return _client
    raise InstagramNotAuthorized("Instagram не авторизован")


def _reset_client() -> None:
    global _client, _logged_in_via
    with _client_lock:
        _client = None
        _logged_in_via = None


def _fetch_threads(client, pending: bool) -> list:
    if pending:
        for method_name in ("direct_pending_inbox", "direct_requests"):
            method = getattr(client, method_name, None)
            if method is None:
                continue
            try:
                return method(amount=20) or []
            except Exception as e:
                if _is_rate_limited(e):
                    raise InstagramRateLimited from e
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
    except Exception as e:
        if _is_rate_limited(e):
            raise InstagramRateLimited from e
        logger.exception("Не удалось получить непрочитанные диалоги Instagram")
        try:
            threads = client.direct_threads(amount=20) or []
        except Exception as e:
            if _is_rate_limited(e):
                raise InstagramRateLimited from e
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

    global _auth_error
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
        raise InstagramRateLimited
    except LoginRequired:
        logger.warning("Сессия Instagram недействительна")
        _reset_client()
        _auth_error = "сессия истекла"
        raise InstagramNotAuthorized
    except InstagramRateLimited:
        raise
    except Exception as e:
        if _is_rate_limited(e):
            raise InstagramRateLimited from e
        raise


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
    else:
        logger.info("Новых сообщений Instagram нет")


def auth_status_text() -> str:
    authorized = is_authorized()
    session_exists = config.IG_SESSION_FILE.exists()
    pending = pending_code_kind()
    lines = [
        f"Аккаунт: {config.IG_USERNAME or 'не задан'}",
    ]
    if authorized:
        lines.append("Статус: авторизован")
        if _logged_in_via == "session":
            lines.append("Сессия: восстановлена из файла после запуска")
        elif _logged_in_via == "password":
            lines.append("Сессия: вход по паролю в этом запуске, сохранена в файл")
        else:
            lines.append("Сессия: активна")
        lines.append("После перезапуска бота входить снова не нужно, пока сессия действительна.")
    else:
        lines.append("Статус: не авторизован")
        lines.append("Сессия: есть файл, но не принят" if session_exists else "Сессия: файла нет")
        if _auth_error:
            lines.append(f"Последняя ошибка: {_auth_error}")
        lines.append("Для входа: /ig_session (cookie sessionid) или /ig_login")
    if _login_busy:
        lines.append("Сейчас выполняется вход...")
    if pending:
        where = CODE_LABELS.get(pending, pending)
        lines.append(f"Ожидается код ({where}): /ig_code 123456")
    return "\n".join(lines)


async def login_instagram() -> str:
    global _login_busy
    if not config.IG_USERNAME or not config.IG_PASSWORD:
        return "В .env не заданы IG_USERNAME или IG_PASSWORD."
    if is_authorized() and not pending_code_kind():
        return "Instagram уже авторизован. Повторный вход не нужен."
    if _login_busy:
        return "Вход уже выполняется. Если пришёл код — отправьте /ig_code 123456"
    _login_busy = True
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _login_by_password_sync)
        return (
            "Вход в Instagram выполнен. Сессия сохранена — "
            "после перезапуска бота входить снова не нужно."
        )
    except InstagramRateLimited:
        return (
            "Instagram снова отклонил вход по паролю (429). "
            "С сервера этот способ сейчас не работает — не повторяйте /ig_login.\n"
            "Войдите через cookie: /ig_session SESSIONID"
        )
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        logger.exception("Ошибка входа в Instagram")
        return f"Вход не удался: {_format_auth_error(e)}"
    finally:
        _login_busy = False


async def login_instagram_session(sessionid: str) -> str:
    global _login_busy
    sessionid = _normalize_sessionid(sessionid)
    if not sessionid:
        return "Использование: /ig_session SESSIONID"
    if is_authorized() and not pending_code_kind():
        return "Instagram уже авторизован. Повторный вход не нужен."
    if _login_busy:
        return "Вход уже выполняется."
    _login_busy = True
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _login_by_sessionid_sync, sessionid)
        return (
            "Вход в Instagram выполнен по sessionid. Сессия сохранена — "
            "после перезапуска бота входить снова не нужно."
        )
    except InstagramRateLimited:
        return (
            "Instagram вернул 429 даже для sessionid. "
            "Подождите сутки и не используйте /ig_login с сервера."
        )
    except RuntimeError as e:
        return str(e)
    except Exception as e:
        logger.exception("Ошибка входа в Instagram по sessionid")
        return f"Вход по sessionid не удался: {_format_auth_error(e)}"
    finally:
        _login_busy = False


async def _notify_code_watch(bot: Bot) -> None:
    last = None
    while True:
        kind = pending_code_kind()
        if kind and kind != last and not _code_notice_sent:
            where = CODE_LABELS.get(kind, kind)
            await _send_to_admin(
                bot,
                f"Instagram запросил код подтверждения ({where}).\n"
                "Отправьте его командой:\n"
                "/ig_code 123456",
            )
        last = kind
        await asyncio.sleep(1)


async def ig_loop(bot: Bot) -> None:
    global _notify_bot, _notify_loop, _session_expired_notified
    config.IG_STORAGE.mkdir(parents=True, exist_ok=True)
    if not config.IG_USERNAME or not config.IG_PASSWORD:
        logger.warning("IG_USERNAME или IG_PASSWORD не заданы — проверка Instagram отключена")
        return
    _notify_bot = bot
    _notify_loop = asyncio.get_running_loop()
    asyncio.create_task(_notify_code_watch(bot))
    try:
        restored = await _notify_loop.run_in_executor(None, _restore_session_sync)
    except InstagramRateLimited:
        restored = False
        logger.warning("Не удалось проверить сессию Instagram из-за 429")
    if restored:
        logger.info(
            "Проверка Instagram запущена для %s (каждые %s сек), сессия восстановлена",
            config.IG_USERNAME,
            config.IG_POLL_INTERVAL,
        )
        await _send_to_admin(
            bot,
            "Instagram: сессия восстановлена после запуска. Повторный вход не нужен.",
        )
    elif _auth_error and "429" in _auth_error:
        logger.info("Instagram: сессию не проверить из-за 429, ожидание /ig_login")
        await _send_to_admin(
            bot,
            "Instagram: сессию сейчас проверить не удалось (429).\n"
            "Не запускайте /ig_login сразу — подождите несколько часов или до завтра, "
            "затем /ig_status и при необходимости /ig_login.",
        )
    else:
        logger.info("Instagram не авторизован, ожидание команды /ig_login")
        await _send_to_admin(
            bot,
            "Instagram не авторизован. Для входа отправьте /ig_login",
        )
    while True:
        remaining = _backoff_until - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)
            continue
        if not is_authorized():
            await asyncio.sleep(config.IG_POLL_INTERVAL)
            continue
        try:
            await process_new_ig(bot)
            _reset_backoff()
        except InstagramNotAuthorized:
            logger.warning("Проверка Instagram остановлена: нет авторизации")
            if not _session_expired_notified:
                _session_expired_notified = True
                await _send_to_admin(
                    bot,
                    "Сессия Instagram истекла. Для повторного входа отправьте /ig_login",
                )
        except InstagramRateLimited:
            _mark_rate_limited()
        except Exception as e:
            if _is_rate_limited(e):
                _mark_rate_limited()
            else:
                logger.exception("Ошибка цикла проверки Instagram")
        await asyncio.sleep(config.IG_POLL_INTERVAL)
