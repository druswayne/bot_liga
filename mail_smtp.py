import logging
from email.encoders import encode_base64
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import aiosmtplib

import config

logger = logging.getLogger(__name__)


async def send_reply(
    *,
    to_addr: str,
    subject: str,
    body: str,
    attachments: list[dict],
    in_reply_to: str | None = None,
) -> None:
    message = MIMEMultipart()
    message["From"] = config.MAIL_USERNAME
    message["To"] = to_addr
    message["Subject"] = subject
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
        message["References"] = in_reply_to
    message.attach(MIMEText(body or "", "plain", "utf-8"))

    for item in attachments:
        path = Path(item["path"])
        if not path.exists():
            continue
        part = MIMEBase("application", "octet-stream")
        part.set_payload(path.read_bytes())
        encode_base64(part)
        filename = item.get("filename") or path.name
        part.add_header("Content-Disposition", "attachment", filename=filename)
        message.attach(part)

    await aiosmtplib.send(
        message,
        hostname=config.SMTP_HOST,
        port=config.SMTP_PORT,
        username=config.MAIL_USERNAME,
        password=config.MAIL_PASSWORD,
        use_tls=True,
        timeout=30,
    )
    logger.info("Ответ отправлен на %s", to_addr)
