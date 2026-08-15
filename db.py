import aiosqlite

from config import DB_PATH, STATUS_IN_PROGRESS, STATUS_PENDING, STATUS_PROCESSED


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    return db


async def init_db() -> None:
    db = await get_db()
    try:
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                authorized INTEGER NOT NULL DEFAULT 0,
                role TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                imap_uid TEXT,
                message_id TEXT UNIQUE,
                subject TEXT,
                from_addr TEXT,
                reply_to TEXT,
                body TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                taken_by TEXT,
                replied_by TEXT,
                deleted INTEGER NOT NULL DEFAULT 0,
                received_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id INTEGER NOT NULL,
                filename TEXT,
                path TEXT NOT NULL,
                mime TEXT,
                is_image INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (email_id) REFERENCES emails(id)
            );

            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                FOREIGN KEY (email_id) REFERENCES emails(id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        await db.commit()
    finally:
        await db.close()


async def get_setting(key: str) -> str | None:
    db = await get_db()
    try:
        cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None
    finally:
        await db.close()


async def set_setting(key: str, value: str) -> None:
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await db.commit()
    finally:
        await db.close()


async def get_user(telegram_id: int) -> dict | None:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def upsert_user(telegram_id: int, username: str | None, authorized: int = 1) -> None:
    db = await get_db()
    try:
        await db.execute(
            """
            INSERT INTO users(telegram_id, username, authorized)
            VALUES(?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = excluded.username,
                authorized = excluded.authorized
            """,
            (telegram_id, username, authorized),
        )
        await db.commit()
    finally:
        await db.close()


async def set_role(telegram_id: int, role: str | None) -> None:
    db = await get_db()
    try:
        await db.execute(
            "UPDATE users SET role = ? WHERE telegram_id = ?",
            (role, telegram_id),
        )
        await db.commit()
    finally:
        await db.close()


async def list_active_users() -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM users WHERE authorized = 1 AND role IS NOT NULL AND role != ''"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def email_exists(message_id: str) -> bool:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT 1 FROM emails WHERE message_id = ?", (message_id,)
        )
        return await cur.fetchone() is not None
    finally:
        await db.close()


async def insert_email(
    *,
    imap_uid: str,
    message_id: str,
    subject: str,
    from_addr: str,
    reply_to: str,
    body: str,
    received_at: str | None,
) -> int:
    db = await get_db()
    try:
        cur = await db.execute(
            """
            INSERT INTO emails(
                imap_uid, message_id, subject, from_addr, reply_to, body, received_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (imap_uid, message_id, subject, from_addr, reply_to, body, received_at),
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def add_attachment(
    email_id: int, filename: str, path: str, mime: str, is_image: bool
) -> None:
    db = await get_db()
    try:
        await db.execute(
            """
            INSERT INTO attachments(email_id, filename, path, mime, is_image)
            VALUES(?, ?, ?, ?, ?)
            """,
            (email_id, filename, path, mime, int(is_image)),
        )
        await db.commit()
    finally:
        await db.close()


async def get_email(email_id: int) -> dict | None:
    db = await get_db()
    try:
        cur = await db.execute("SELECT * FROM emails WHERE id = ?", (email_id,))
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def list_open_emails(limit: int = 15) -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            """
            SELECT * FROM emails
            WHERE deleted = 0
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_attachments(email_id: int) -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM attachments WHERE email_id = ?", (email_id,)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def take_email(email_id: int, role: str) -> bool:
    db = await get_db()
    try:
        cur = await db.execute(
            """
            UPDATE emails
            SET status = ?, taken_by = ?
            WHERE id = ? AND deleted = 0 AND status = 'pending'
            """,
            (STATUS_IN_PROGRESS, role, email_id),
        )
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


async def release_email(email_id: int, role: str) -> bool:
    db = await get_db()
    try:
        cur = await db.execute(
            """
            UPDATE emails
            SET status = ?, taken_by = NULL
            WHERE id = ? AND deleted = 0 AND status = ? AND taken_by = ?
            """,
            (STATUS_PENDING, email_id, STATUS_IN_PROGRESS, role),
        )
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


async def mark_processed(email_id: int, role: str) -> None:
    db = await get_db()
    try:
        await db.execute(
            """
            UPDATE emails
            SET status = ?, replied_by = ?, taken_by = COALESCE(taken_by, ?)
            WHERE id = ?
            """,
            (STATUS_PROCESSED, role, role, email_id),
        )
        await db.commit()
    finally:
        await db.close()


async def soft_delete(email_id: int) -> None:
    db = await get_db()
    try:
        await db.execute("UPDATE emails SET deleted = 1 WHERE id = ?", (email_id,))
        await db.commit()
    finally:
        await db.close()


async def add_notification(email_id: int, chat_id: int, message_id: int) -> None:
    db = await get_db()
    try:
        await db.execute(
            """
            INSERT INTO notifications(email_id, chat_id, message_id)
            VALUES(?, ?, ?)
            """,
            (email_id, chat_id, message_id),
        )
        await db.commit()
    finally:
        await db.close()


async def get_notifications(email_id: int) -> list[dict]:
    db = await get_db()
    try:
        cur = await db.execute(
            "SELECT * FROM notifications WHERE email_id = ?", (email_id,)
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()
