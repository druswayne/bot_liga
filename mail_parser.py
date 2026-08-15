import re
from email.utils import parseaddr

from bs4 import BeautifulSoup

from config import FORWARD_FROM

EMAIL_IN_BODY = re.compile(
    r"(?:e-?mail)\s*:\s*<?([A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,})>?",
    re.IGNORECASE,
)


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html or "", "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(["p", "div", "tr", "li"]):
        tag.append("\n")
    text = soup.get_text()
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def extract_body(text: str | None, html: str | None) -> str:
    if text and text.strip():
        return text.strip()
    if html:
        return html_to_text(html)
    return ""


def parse_from(from_raw: str) -> str:
    _name, addr = parseaddr(from_raw or "")
    return (addr or from_raw or "").strip()


def extract_reply_to(from_addr: str, body: str) -> str:
    addr = parse_from(from_addr)
    if addr.lower() == FORWARD_FROM.lower():
        match = EMAIL_IN_BODY.search(body or "")
        if match:
            return match.group(1)
    return addr
