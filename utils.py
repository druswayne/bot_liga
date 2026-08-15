from html import escape

from config import folder_label, status_label


def split_text(text: str, limit: int = 4000) -> list[str]:
    text = text or ""
    if len(text) <= limit:
        return [text] if text else [""]
    chunks: list[str] = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks


def format_card(email: dict, is_new: bool = False) -> str:
    title = "📧 Новое письмо" if is_new else "📧 Письмо"
    subject = escape(email.get("subject") or "(без темы)")
    sender = escape(email.get("from_addr") or "неизвестно")
    folder = escape(folder_label(email.get("imap_folder")))
    status = escape(
        status_label(
            email.get("status"),
            email.get("taken_by"),
            email.get("replied_by"),
        )
    )
    lines = [
        f"{title}\n",
        f"<b>Тема:</b> {subject}",
        f"<b>От:</b> {sender}",
    ]
    if folder:
        lines.append(f"<b>Папка:</b> {folder}")
    lines.append(f"<b>Статус:</b> {status}")
    return "\n".join(lines)


def _ig_sender_label(item: dict) -> str:
    username = (item.get("sender_username") or "").strip()
    name = (item.get("sender_name") or "").strip()
    if username and name and name.lower() != username.lower():
        return f"@{username} ({name})"
    if username:
        return f"@{username}"
    return name or "неизвестно"


def format_ig_card(item: dict, is_new: bool = False) -> str:
    title = "📸 Новое сообщение Instagram" if is_new else "📸 Instagram"
    sender = escape(_ig_sender_label(item))
    status = escape(
        status_label(
            item.get("status"),
            item.get("taken_by"),
            item.get("replied_by"),
        )
    )
    lines = [
        f"{title}\n",
        f"<b>От:</b> {sender}",
    ]
    if item.get("is_pending"):
        lines.append("<b>Входящие:</b> запрос в сообщениях")
    lines.append(f"<b>Статус:</b> {status}")
    return "\n".join(lines)
