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
