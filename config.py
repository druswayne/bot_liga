import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()

MAIL_USERNAME = os.getenv("MAIL_USERNAME", "info@liga-znatokov.by").strip()
MAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "").strip()
IMAP_HOST = os.getenv("IMAP_HOST", "mail.liga-znatokov.by").strip()
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
SMTP_HOST = os.getenv("SMTP_HOST", "mail.liga-znatokov.by").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
MAIL_POLL_INTERVAL = int(os.getenv("MAIL_POLL_INTERVAL", "30"))
MAIL_FOLDERS = [
    name.strip()
    for name in os.getenv(
        "MAIL_FOLDERS",
        "INBOX.Drafts.metodicheskaya_chast,INBOX.Drafts.tekhnicheskaya_chast,INBOX.Drafts.drugoe",
    ).split(",")
    if name.strip()
]

IG_USERNAME = os.getenv("IG_USERNAME", "").strip()
IG_PASSWORD = os.getenv("IG_PASSWORD", "").strip()
IG_POLL_INTERVAL = int(os.getenv("IG_POLL_INTERVAL", "90"))

FOLDER_LABELS = {
    "metodicheskaya_chast": "Методическая часть",
    "tekhnicheskaya_chast": "Техническая часть",
    "drugoe": "Другое",
    "INBOX": "Входящие",
    "INBOX.Методическая часть": "Методическая часть",
}


def folder_label(folder: str | None) -> str:
    if not folder:
        return ""
    if folder in FOLDER_LABELS:
        return FOLDER_LABELS[folder]
    short = folder.split(".")[-1]
    return FOLDER_LABELS.get(short, folder)

FORWARD_FROM = "th@liga-znatokov.by"

DB_PATH = BASE_DIR / "bot.db"
STORAGE_DIR = BASE_DIR / "storage"
MAIL_STORAGE = STORAGE_DIR / "mail"
REPLY_STORAGE = STORAGE_DIR / "replies"
IG_STORAGE = STORAGE_DIR / "instagram"
IG_SESSION_FILE = STORAGE_DIR / "ig_session.json"

ROLES = {
    "andrey": {
        "key": "andrey",
        "name": "Андрей",
        "in_work": "В работе у Андрея",
        "replied": "Ответил Андрей",
    },
    "oleg": {
        "key": "oleg",
        "name": "Олег",
        "in_work": "В работе у Олега",
        "replied": "Ответил Олег",
    },
    "marina": {
        "key": "marina",
        "name": "Марина",
        "in_work": "В работе у Марины",
        "replied": "Ответила Марина",
    },
}

STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_PROCESSED = "processed"


def status_label(status: str, taken_by: str | None = None, replied_by: str | None = None) -> str:
    if status == STATUS_PENDING:
        return "⏳ Ожидает"
    if status == STATUS_IN_PROGRESS:
        role = ROLES.get(taken_by or "")
        return f"🔄 {role['in_work']}" if role else "🔄 В работе"
    if status == STATUS_PROCESSED:
        role = ROLES.get(replied_by or "")
        return f"✅ {role['replied']}" if role else "✅ Обработано"
    return status
