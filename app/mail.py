"""SMTP send + IMAP poll for RFQ and chat replies."""

from __future__ import annotations

import email
import imaplib
import logging
import re
import smtplib
import socket
import ssl
from datetime import date, datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.utils import formataddr, make_msgid, parseaddr
from typing import Any

from app.config import get_settings
from app.db import get_db

log = logging.getLogger("mail")

RFQ_TOKEN_RE = re.compile(r"\[RFQ-([A-Za-z0-9]{6,12})\]", re.I)
CHAT_TOKEN_RE = re.compile(r"\[CHAT-([A-Za-z0-9]{6,12})\]", re.I)
TOKEN_RE = RFQ_TOKEN_RE  # legacy
IMAP_TIMEOUT_SEC = 25
MAX_FETCH = 40


def _decode_hdr(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw or ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _use_starttls(port: int) -> bool:
    """465 = implicit SSL; 587/25/2525 = plain then STARTTLS."""
    return int(port) in (25, 587, 2525)


def _smtp_send(msg: EmailMessage, *, settings) -> None:
    """Connect via SMTP_SSL (465) or SMTP+STARTTLS (587) and send."""
    host = (settings.mail_smtp_host or "").strip() or "smtp.yandex.ru"
    port = int(settings.mail_smtp_port or 465)
    user = settings.mail_user.strip()
    password = settings.mail_password
    context = ssl.create_default_context()
    if _use_starttls(port):
        with smtplib.SMTP(host, port, timeout=IMAP_TIMEOUT_SEC) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=IMAP_TIMEOUT_SEC) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)


def _smtp_login_check(*, settings) -> None:
    host = (settings.mail_smtp_host or "").strip() or "smtp.yandex.ru"
    port = int(settings.mail_smtp_port or 465)
    user = settings.mail_user.strip()
    password = settings.mail_password
    context = ssl.create_default_context()
    if _use_starttls(port):
        with smtplib.SMTP(host, port, timeout=IMAP_TIMEOUT_SEC) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            smtp.login(user, password)
    else:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=IMAP_TIMEOUT_SEC) as smtp:
            smtp.login(user, password)


def send_email(
    *,
    to_addr: str,
    subject: str,
    body: str,
    from_name: str | None = None,
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> str:
    """Send email. attachments: list of (filename, content_bytes, mime)."""
    settings = get_settings()
    if not settings.mail_ready:
        raise RuntimeError("Почта не настроена (MAIL_USER / MAIL_PASSWORD)")

    display_name = (from_name or settings.sender_name or "").strip() or settings.sender_name
    msg = EmailMessage()
    from_addr = settings.mail_from_addr
    msg["From"] = formataddr((display_name, from_addr))
    msg["To"] = to_addr
    msg["Subject"] = subject
    message_id = make_msgid(domain=from_addr.split("@")[-1] if "@" in from_addr else "localhost")
    msg["Message-ID"] = message_id
    msg.set_content(body, charset="utf-8")

    for filename, content, mime in attachments or []:
        if not content:
            continue
        maintype, _, subtype = (mime or "application/octet-stream").partition("/")
        if not subtype:
            maintype, subtype = "application", "octet-stream"
        msg.add_attachment(
            content,
            maintype=maintype,
            subtype=subtype,
            filename=filename,
        )

    _smtp_send(msg, settings=settings)
    return message_id


def extract_token(text: str) -> str | None:
    m = RFQ_TOKEN_RE.search(text or "")
    return m.group(1).upper() if m else None


def extract_chat_token(text: str) -> str | None:
    m = CHAT_TOKEN_RE.search(text or "")
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
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                html = payload.decode(charset, errors="replace")
                from app.parse_quotes import strip_mail_noise

                return strip_mail_noise(html)
        return ""
    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except Exception:
        return payload.decode("utf-8", errors="replace")


def extract_attachments(msg: Message) -> list[tuple[str, bytes, str]]:
    out: list[tuple[str, bytes, str]] = []
    if not msg.is_multipart():
        return out
    for part in msg.walk():
        disp = (part.get("Content-Disposition") or "").lower()
        ctype = part.get_content_type() or "application/octet-stream"
        filename = part.get_filename()
        if filename:
            filename = _decode_hdr(filename)
        is_attach = "attachment" in disp or bool(filename)
        if not is_attach:
            continue
        if ctype.startswith("text/") and not filename:
            continue
        payload = part.get_payload(decode=True) or b""
        if not payload:
            continue
        name = filename or f"attachment-{len(out) + 1}"
        out.append((name, payload, ctype))
        if len(out) >= 8:
            break
    return out


def _imap_connect(settings):
    mail = imaplib.IMAP4_SSL(settings.mail_imap_host, settings.mail_imap_port, timeout=IMAP_TIMEOUT_SEC)
    mail.login(settings.mail_user.strip(), settings.mail_password)
    if mail.sock:
        mail.sock.settimeout(IMAP_TIMEOUT_SEC)
    return mail


def _yandex_auth_hint(err: str) -> str:
    e = (err or "").lower()
    if "authenticationfailed" in e or "invalid credentials" in e:
        return (
            "Яндекс отклонил пароль. Нужен пароль приложения (16 символов) "
            "в настройках Яндекс ID → Пароли приложений, не обычный пароль от почты."
        )
    if "timed out" in e or "timeout" in e:
        return "Таймаут соединения с почтой. Проверьте интернет/VPN и повторите."
    return err


def test_mail_login() -> dict[str, Any]:
    get_settings.cache_clear()
    settings = get_settings()
    if not settings.mail_ready:
        return {"ok": False, "error": "Заполните MAIL_USER и MAIL_PASSWORD в .env"}
    try:
        mail = _imap_connect(settings)
        mail.logout()
    except Exception as exc:
        return {"ok": False, "error": _yandex_auth_hint(str(exc))}
    try:
        _smtp_login_check(settings=settings)
    except Exception as exc:
        return {
            "ok": False,
            "error": (
                f"SMTP ({settings.mail_smtp_host}:{settings.mail_smtp_port}): "
                f"{_yandex_auth_hint(str(exc))}"
            ),
        }
    mode = "STARTTLS" if _use_starttls(int(settings.mail_smtp_port or 465)) else "SSL"
    return {
        "ok": True,
        "detail": (
            f"IMAP и SMTP вход успешны "
            f"({settings.mail_smtp_host}:{settings.mail_smtp_port}, {mode})"
        ),
    }


def poll_replies(
    *,
    force_seen: bool = False,
    since_days: int = 14,
    max_messages: int = MAX_FETCH,
) -> dict[str, Any]:
    """Poll IMAP for RFQ-[token] and CHAT-[token] replies; save attachments."""
    from app.files import save_upload
    from app.parse_quotes import parse_and_store_reply

    get_settings.cache_clear()
    settings = get_settings()
    if not settings.mail_ready:
        return {"ok": False, "error": "Почта не настроена", "checked": 0, "saved": 0}

    checked = 0
    saved = 0
    errors: list[str] = []
    mail = None

    try:
        mail = _imap_connect(settings)
    except Exception as exc:
        log.exception("IMAP login failed")
        return {"ok": False, "error": _yandex_auth_hint(str(exc)), "checked": 0, "saved": 0}

    try:
        folder = settings.mail_imap_folder or "INBOX"
        typ, _ = mail.select(folder)
        if typ != "OK":
            return {"ok": False, "error": f"Не удалось открыть папку {folder}", "checked": 0, "saved": 0}

        if force_seen:
            since = (date.today() - timedelta(days=max(1, since_days))).strftime("%d-%b-%Y")
            criteria = f"(SINCE {since})"
        else:
            criteria = "UNSEEN"

        typ, data = mail.search(None, criteria)
        if typ != "OK" or not data or not data[0]:
            return {"ok": True, "checked": 0, "saved": 0, "errors": [], "criteria": criteria}

        ids = data[0].split()
        if len(ids) > max_messages:
            ids = ids[-max_messages:]

        conn = get_db()
        try:
            for num in ids:
                checked += 1
                try:
                    typ, hdr_data = mail.fetch(
                        num, "(BODY.PEEK[HEADER.FIELDS (SUBJECT MESSAGE-ID FROM)])"
                    )
                    if typ != "OK" or not hdr_data or not hdr_data[0]:
                        continue
                    hdr_raw = hdr_data[0][1]
                    if not isinstance(hdr_raw, (bytes, bytearray)):
                        continue
                    hdr_msg = email.message_from_bytes(hdr_raw)
                    subject = _decode_hdr(hdr_msg.get("Subject"))
                    rfq_token = extract_token(subject)
                    chat_token = extract_chat_token(subject)
                    if not rfq_token and not chat_token:
                        continue

                    message_id = (hdr_msg.get("Message-ID") or "").strip() or (
                        f"imap-{num.decode() if isinstance(num, bytes) else num}"
                    )

                    if rfq_token:
                        row = conn.execute(
                            "SELECT id, rfq_id FROM rfq_messages WHERE upper(token)=?",
                            (rfq_token.upper(),),
                        ).fetchone()
                        if not row:
                            continue
                        exists = conn.execute(
                            "SELECT id FROM quote_replies WHERE message_id=?",
                            (message_id,),
                        ).fetchone()
                        if exists:
                            if not force_seen:
                                mail.store(num, "+FLAGS", "\\Seen")
                            continue

                        typ, msg_data = mail.fetch(num, "(RFC822)")
                        if typ != "OK" or not msg_data or not msg_data[0]:
                            continue
                        raw = msg_data[0][1]
                        if not isinstance(raw, (bytes, bytearray)):
                            continue
                        msg = email.message_from_bytes(raw)
                        from_addr = parseaddr(msg.get("From") or "")[1]
                        body = _get_text_body(msg)
                        cur = conn.execute(
                            """
                            INSERT INTO quote_replies
                              (rfq_message_id, message_id, from_addr, subject, raw_body,
                               parse_status, received_at, is_read)
                            VALUES (?, ?, ?, ?, ?, 'pending', ?, 0)
                            """,
                            (row["id"], message_id, from_addr, subject, body, _now()),
                        )
                        reply_id = int(cur.lastrowid)
                        for fname, payload, mime in extract_attachments(msg):
                            try:
                                save_upload(
                                    conn,
                                    owner_type="quote_reply",
                                    owner_id=reply_id,
                                    filename=fname,
                                    data=payload,
                                    mime=mime,
                                )
                            except Exception as exc:
                                log.warning("Skip attachment %s: %s", fname, exc)
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
                        continue

                    chat = conn.execute(
                        "SELECT id FROM supplier_chats WHERE upper(token)=?",
                        (chat_token.upper(),),
                    ).fetchone()
                    if not chat:
                        continue
                    exists = conn.execute(
                        """
                        SELECT id FROM supplier_chat_messages
                        WHERE message_id=? AND direction='in'
                        """,
                        (message_id,),
                    ).fetchone()
                    if exists:
                        if not force_seen:
                            mail.store(num, "+FLAGS", "\\Seen")
                        continue

                    typ, msg_data = mail.fetch(num, "(RFC822)")
                    if typ != "OK" or not msg_data or not msg_data[0]:
                        continue
                    raw = msg_data[0][1]
                    if not isinstance(raw, (bytes, bytearray)):
                        continue
                    msg = email.message_from_bytes(raw)
                    from_addr = parseaddr(msg.get("From") or "")[1]
                    body = _get_text_body(msg)
                    cur = conn.execute(
                        """
                        INSERT INTO supplier_chat_messages
                          (chat_id, direction, kind, subject, body, message_id, from_addr,
                           source_channel, status, created_at, is_read)
                        VALUES (?, 'in', 'email', ?, ?, ?, ?, 'email', 'received', ?, 0)
                        """,
                        (chat["id"], subject, body, message_id, from_addr, _now()),
                    )
                    mid = int(cur.lastrowid)
                    for fname, payload, mime in extract_attachments(msg):
                        try:
                            save_upload(
                                conn,
                                owner_type="chat_message",
                                owner_id=mid,
                                filename=fname,
                                data=payload,
                                mime=mime,
                            )
                        except Exception as exc:
                            log.warning("Skip chat attachment %s: %s", fname, exc)
                    if from_addr and "@" in from_addr:
                        conn.execute(
                            """
                            UPDATE supplier_chats
                            SET reply_email=CASE WHEN reply_email='' THEN ? ELSE reply_email END,
                                updated_at=?
                            WHERE id=?
                            """,
                            (from_addr, _now(), chat["id"]),
                        )
                    else:
                        conn.execute(
                            "UPDATE supplier_chats SET updated_at=? WHERE id=?",
                            (_now(), chat["id"]),
                        )
                    conn.commit()
                    try:
                        from app.parse_quotes import parse_and_store_chat_message

                        parse_and_store_chat_message(conn, mid)
                        conn.commit()
                    except Exception as exc:
                        log.exception("Parse chat reply failed")
                        errors.append(str(exc)[:200])
                        conn.rollback()
                    saved += 1
                    mail.store(num, "+FLAGS", "\\Seen")
                except socket.timeout:
                    errors.append("Таймаут при чтении письма — остановлено")
                    break
                except Exception as exc:
                    log.exception("Skip mail %s", num)
                    errors.append(str(exc)[:200])
                    continue
        finally:
            conn.close()
    except socket.timeout:
        return {
            "ok": False,
            "error": _yandex_auth_hint("timed out"),
            "checked": checked,
            "saved": saved,
        }
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass

    return {"ok": True, "checked": checked, "saved": saved, "errors": errors}
