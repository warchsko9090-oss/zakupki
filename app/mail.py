"""SMTP send + IMAP poll for RFQ replies."""

from __future__ import annotations

import email
import imaplib
import logging
import re
import smtplib
import ssl
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.utils import formataddr, make_msgid, parseaddr
from typing import Any

from app.config import get_settings
from app.db import get_db

log = logging.getLogger("mail")

TOKEN_RE = re.compile(r"\[RFQ-([A-Za-z0-9]{6,12})\]", re.I)


def _decode_hdr(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw or ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def send_email(*, to_addr: str, subject: str, body: str) -> str:
    settings = get_settings()
    if not settings.mail_ready:
        raise RuntimeError("Почта не настроена (MAIL_USER / MAIL_PASSWORD)")

    msg = EmailMessage()
    from_addr = settings.mail_from_addr
    msg["From"] = formataddr((settings.sender_name, from_addr))
    msg["To"] = to_addr
    msg["Subject"] = subject
    message_id = make_msgid(domain=from_addr.split("@")[-1] if "@" in from_addr else "localhost")
    msg["Message-ID"] = message_id
    msg.set_content(body, charset="utf-8")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(settings.mail_smtp_host, settings.mail_smtp_port, context=context) as smtp:
        smtp.login(settings.mail_user, settings.mail_password)
        smtp.send_message(msg)
    return message_id


def extract_token(text: str) -> str | None:
    m = TOKEN_RE.search(text or "")
    return m.group(1).upper() if m else None


def _get_text_body(msg: Message) -> str:
    if msg.is_multipart():
        parts: list[str] = []
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            if ctype == "text/plain":
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                try:
                    parts.append(payload.decode(charset, errors="replace"))
                except Exception:
                    parts.append(payload.decode("utf-8", errors="replace"))
        if parts:
            return "\n".join(parts)
        # fallback html stripped lightly
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                html = payload.decode(charset, errors="replace")
                return re.sub(r"<[^>]+>", " ", html)
        return ""
    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except Exception:
        return payload.decode("utf-8", errors="replace")


def poll_replies(*, force_seen: bool = False) -> dict[str, Any]:
    """Fetch new mails, match RFQ tokens, store replies."""
    settings = get_settings()
    if not settings.mail_ready:
        return {"ok": False, "error": "Почта не настроена", "checked": 0, "saved": 0}

    from app.parse_quotes import parse_and_store_reply

    checked = 0
    saved = 0
    errors: list[str] = []

    try:
        mail = imaplib.IMAP4_SSL(settings.mail_imap_host, settings.mail_imap_port)
        mail.login(settings.mail_user, settings.mail_password)
    except Exception as exc:
        log.exception("IMAP login failed")
        return {"ok": False, "error": str(exc), "checked": 0, "saved": 0}

    try:
        folder = settings.mail_imap_folder or "INBOX"
        typ, _ = mail.select(folder)
        if typ != "OK":
            return {"ok": False, "error": f"Не удалось открыть папку {folder}", "checked": 0, "saved": 0}

        criteria = "ALL" if force_seen else "UNSEEN"
        typ, data = mail.search(None, criteria)
        if typ != "OK" or not data or not data[0]:
            return {"ok": True, "checked": 0, "saved": 0, "errors": []}

        ids = data[0].split()
        conn = get_db()
        try:
            for num in ids:
                checked += 1
                typ, msg_data = mail.fetch(num, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                if not isinstance(raw, (bytes, bytearray)):
                    continue
                msg = email.message_from_bytes(raw)
                subject = _decode_hdr(msg.get("Subject"))
                from_addr = parseaddr(msg.get("From") or "")[1]
                message_id = (msg.get("Message-ID") or "").strip() or f"imap-{num.decode()}"
                body = _get_text_body(msg)

                token = extract_token(subject) or extract_token(body)
                if not token:
                    continue

                row = conn.execute(
                    "SELECT id, rfq_id FROM rfq_messages WHERE upper(token)=?",
                    (token.upper(),),
                ).fetchone()
                if not row:
                    continue

                exists = conn.execute(
                    "SELECT id FROM quote_replies WHERE message_id=?",
                    (message_id,),
                ).fetchone()
                if exists:
                    mail.store(num, "+FLAGS", "\\Seen")
                    continue

                cur = conn.execute(
                    """
                    INSERT INTO quote_replies
                      (rfq_message_id, message_id, from_addr, subject, raw_body, parse_status, received_at)
                    VALUES (?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (row["id"], message_id, from_addr, subject, body, _now()),
                )
                reply_id = cur.lastrowid
                conn.execute(
                    "UPDATE rfq_messages SET status='replied' WHERE id=? AND status!='error'",
                    (row["id"],),
                )
                conn.execute(
                    "UPDATE rfqs SET status='collecting' WHERE id=? AND status='sent'",
                    (row["rfq_id"],),
                )
                conn.commit()
                try:
                    parse_and_store_reply(conn, reply_id)
                    conn.commit()
                except Exception as exc:
                    log.exception("Parse reply failed")
                    errors.append(str(exc))
                    conn.rollback()
                saved += 1
                mail.store(num, "+FLAGS", "\\Seen")
        finally:
            conn.close()
    finally:
        try:
            mail.logout()
        except Exception:
            pass

    return {"ok": True, "checked": checked, "saved": saved, "errors": errors}
