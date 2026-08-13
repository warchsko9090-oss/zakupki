"""Standalone supplier chats (outside RFQ) + company bank accounts."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any

from app.db import rows_to_dicts
from app.files import file_payloads_for_send, kind_label, list_files, list_files_for_owners, save_upload
from app.mail import send_email


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _token(n: int = 8) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))


# ── Company bank accounts (max 5) ─────────────────────────

MAX_COMPANY_ACCOUNTS = 5


def list_company_accounts(conn) -> list[dict]:
    return rows_to_dicts(
        conn.execute(
            "SELECT * FROM company_accounts ORDER BY is_default DESC, sort_order, id"
        ).fetchall()
    )


def get_company_account(conn, account_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM company_accounts WHERE id=?", (account_id,)).fetchone()
    return dict(row) if row else None


def format_company_requisites(acc: dict) -> str:
    lines = ["Реквизиты для оплаты / договора:"]
    if acc.get("legal_name"):
        lines.append(f"Получатель: {acc['legal_name']}")
    if acc.get("inn"):
        lines.append(f"ИНН: {acc['inn']}" + (f"  КПП: {acc['kpp']}" if acc.get("kpp") else ""))
    elif acc.get("kpp"):
        lines.append(f"КПП: {acc['kpp']}")
    if acc.get("bank_name"):
        lines.append(f"Банк: {acc['bank_name']}")
    if acc.get("bik"):
        lines.append(f"БИК: {acc['bik']}")
    if acc.get("checking_account"):
        lines.append(f"р/с: {acc['checking_account']}")
    if acc.get("corr_account"):
        lines.append(f"к/с: {acc['corr_account']}")
    if acc.get("address"):
        lines.append(f"Адрес: {acc['address']}")
    return "\n".join(lines)


def save_company_account(conn, data: dict[str, Any], account_id: int | None = None) -> int:
    label = (data.get("label") or "").strip() or (data.get("legal_name") or "").strip() or "Компания"
    fields = {
        "label": label,
        "legal_name": (data.get("legal_name") or "").strip(),
        "inn": (data.get("inn") or "").strip(),
        "kpp": (data.get("kpp") or "").strip(),
        "bank_name": (data.get("bank_name") or "").strip(),
        "bik": (data.get("bik") or "").strip(),
        "checking_account": (data.get("checking_account") or "").strip(),
        "corr_account": (data.get("corr_account") or "").strip(),
        "address": (data.get("address") or "").strip(),
        "is_default": 1 if data.get("is_default") else 0,
        "sort_order": int(data.get("sort_order") or 0),
    }
    if account_id:
        conn.execute(
            """
            UPDATE company_accounts SET
              label=?, legal_name=?, inn=?, kpp=?, bank_name=?, bik=?,
              checking_account=?, corr_account=?, address=?, is_default=?, sort_order=?
            WHERE id=?
            """,
            (
                fields["label"],
                fields["legal_name"],
                fields["inn"],
                fields["kpp"],
                fields["bank_name"],
                fields["bik"],
                fields["checking_account"],
                fields["corr_account"],
                fields["address"],
                fields["is_default"],
                fields["sort_order"],
                account_id,
            ),
        )
        aid = account_id
    else:
        n = conn.execute("SELECT COUNT(*) AS c FROM company_accounts").fetchone()["c"]
        if int(n) >= MAX_COMPANY_ACCOUNTS:
            raise ValueError(f"Можно сохранить не больше {MAX_COMPANY_ACCOUNTS} компаний")
        cur = conn.execute(
            """
            INSERT INTO company_accounts
              (label, legal_name, inn, kpp, bank_name, bik, checking_account, corr_account,
               address, is_default, sort_order, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fields["label"],
                fields["legal_name"],
                fields["inn"],
                fields["kpp"],
                fields["bank_name"],
                fields["bik"],
                fields["checking_account"],
                fields["corr_account"],
                fields["address"],
                fields["is_default"],
                fields["sort_order"],
                _now(),
            ),
        )
        aid = int(cur.lastrowid)
    if fields["is_default"]:
        conn.execute("UPDATE company_accounts SET is_default=0 WHERE id!=?", (aid,))
    return aid


def delete_company_account(conn, account_id: int) -> None:
    conn.execute("DELETE FROM company_accounts WHERE id=?", (account_id,))


# ── Supplier chats ────────────────────────────────────────


def list_chats(conn) -> list[dict]:
    rows = rows_to_dicts(
        conn.execute(
            """
            SELECT c.*, s.name AS supplier_name, s.email AS supplier_email,
                   (SELECT COUNT(*) FROM supplier_chat_messages m WHERE m.chat_id=c.id) AS msg_count,
                   (SELECT COUNT(*) FROM message_files f
                      JOIN supplier_chat_messages m ON m.id=f.owner_id AND f.owner_type='chat_message'
                    WHERE m.chat_id=c.id) AS file_count,
                   (SELECT COUNT(*) FROM supplier_chat_messages m
                    WHERE m.chat_id=c.id AND m.direction='in' AND m.is_read=0) AS unread_count
            FROM supplier_chats c
            JOIN suppliers s ON s.id=c.supplier_id
            ORDER BY c.updated_at DESC, c.id DESC
            """
        ).fetchall()
    )
    return rows


def mark_chat_messages_read(conn, chat_id: int) -> int:
    cur = conn.execute(
        """
        UPDATE supplier_chat_messages
        SET is_read=1
        WHERE chat_id=? AND direction='in' AND is_read=0
        """,
        (chat_id,),
    )
    return int(cur.rowcount or 0)


def count_unread_chat_messages(conn, chat_id: int | None = None) -> int:
    if chat_id:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM supplier_chat_messages
            WHERE chat_id=? AND direction='in' AND is_read=0
            """,
            (chat_id,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM supplier_chat_messages
            WHERE direction='in' AND is_read=0
            """
        ).fetchone()
    return int(row["c"] if row else 0)


def _item_display_name(it: dict) -> str:
    name = (it.get("product_name") or it.get("custom_name") or "").strip()
    return name or "Позиция"


def get_chat(conn, chat_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT c.*, s.name AS supplier_name, s.email AS supplier_email,
               s.contact_person
        FROM supplier_chats c
        JOIN suppliers s ON s.id=c.supplier_id
        WHERE c.id=?
        """,
        (chat_id,),
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    line_items = rows_to_dicts(
        conn.execute(
            """
            SELECT i.*,
                   COALESCE(NULLIF(i.custom_name,''), p.name, 'Позиция') AS product_name,
                   COALESCE(cat.name, '') AS category_name
            FROM supplier_chat_items i
            LEFT JOIN products p ON p.id=i.product_id
            LEFT JOIN categories cat ON cat.id=p.category_id
            WHERE i.chat_id=?
            ORDER BY i.id
            """,
            (chat_id,),
        ).fetchall()
    )
    # Never use key "items" — conflicts with dict.items in Jinja
    data["line_items"] = line_items
    messages = rows_to_dicts(
        conn.execute(
            """
            SELECT * FROM supplier_chat_messages
            WHERE chat_id=?
            ORDER BY created_at ASC, id ASC
            """,
            (chat_id,),
        ).fetchall()
    )
    files_by = list_files_for_owners(conn, "chat_message", [m["id"] for m in messages])
    for m in messages:
        m["files"] = files_by.get(int(m["id"]), [])
        for f in m["files"]:
            f["kind_label"] = kind_label(f.get("kind") or "")
        raw = (m.get("comparison_json") or "").strip()
        m["comparison"] = None
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    m["comparison"] = parsed
            except json.JSONDecodeError:
                pass
        if m.get("direction") == "in":
            from app.parse_quotes import strip_mail_noise

            m["display_body"] = strip_mail_noise(m.get("body") or "")
            m["is_new"] = not bool(m.get("is_read", 1))
        else:
            m["display_body"] = m.get("body") or ""
            m["is_new"] = False
    data["messages"] = messages
    data["active_email"] = (data.get("reply_email") or data.get("supplier_email") or "").strip()
    data["all_files"] = []
    for m in messages:
        for f in m["files"]:
            data["all_files"].append({**f, "message_id": m["id"], "direction": m["direction"]})
    data["analytics"] = build_chat_analytics(conn, data)
    return data


def create_chat(
    conn,
    *,
    supplier_id: int,
    title: str = "",
    notes: str = "",
    letter_conditions: str = "",
    items: list[dict[str, Any]] | None = None,
) -> int:
    supplier = conn.execute("SELECT * FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
    if not supplier:
        raise ValueError("Поставщик не найден")
    items = items or []
    if not title.strip():
        names = []
        for it in items[:3]:
            if it.get("custom_name"):
                names.append(str(it["custom_name"]).strip())
            elif it.get("product_id"):
                p = conn.execute(
                    "SELECT name FROM products WHERE id=?", (int(it["product_id"]),)
                ).fetchone()
                if p:
                    names.append(p["name"])
        title = ", ".join(names) if names else f"Чат с {supplier['name']}"
    token = _token()
    now = _now()
    cur = conn.execute(
        """
        INSERT INTO supplier_chats
          (supplier_id, title, token, reply_email, status, notes, letter_conditions,
           created_at, updated_at)
        VALUES (?, ?, ?, '', 'open', ?, ?, ?, ?)
        """,
        (
            supplier_id,
            title.strip(),
            token,
            notes.strip(),
            (letter_conditions or "").strip(),
            now,
            now,
        ),
    )
    chat_id = int(cur.lastrowid)
    _insert_chat_items(conn, chat_id, items)
    return chat_id


def _insert_chat_items(conn, chat_id: int, items: list[dict[str, Any]]) -> None:
    for it in items:
        pid = it.get("product_id")
        pid_i = int(pid) if pid not in (None, "", 0, "0") else None
        custom = (it.get("custom_name") or it.get("name") or "").strip()
        if not pid_i and not custom:
            continue
        if pid_i and not custom:
            prow = conn.execute("SELECT name FROM products WHERE id=?", (pid_i,)).fetchone()
            custom = (prow["name"] if prow else "") or ""
        conn.execute(
            """
            INSERT INTO supplier_chat_items
              (chat_id, product_id, custom_name, quantity, unit, note)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                pid_i,
                custom,
                float(it.get("quantity") or 1),
                (it.get("unit") or "шт").strip(),
                (it.get("note") or "").strip(),
            ),
        )


def build_opening_text(
    *,
    supplier_name: str,
    contact: str,
    items: list[dict[str, Any]],
    profile: dict[str, str],
    token_mark: str,
    conditions: str = "",
) -> str:
    lines = [
        f"Добрый день" + (f", {contact}" if contact else "") + "!",
        "",
        f"Меня зовут {profile['sender_name']}"
        + (f", {profile['sender_position']}" if profile.get("sender_position") else "")
        + (f" ({profile['sender_company']})" if profile.get("sender_company") else "")
        + ".",
    ]
    if items:
        lines.append("Прошу сориентировать по наличию и цене на позиции:")
        lines.append("")
        for i, it in enumerate(items, 1):
            name = (it.get("custom_name") or it.get("name") or it.get("product_name") or "Позиция").strip()
            line = f"{i}. {name} — {it.get('quantity') or 1} {it.get('unit') or 'шт'}"
            if it.get("note"):
                line += f" ({it['note']})"
            lines.append(line)
        lines.append("")
    if conditions.strip():
        lines.append("Дополнительные условия и требования:")
        lines.append(conditions.strip())
        lines.append("")
    lines.extend(
        [
            "Буду признателен за КП / счёт или уточнения.",
            "",
            "С уважением,",
            profile["sender_name"],
        ]
    )
    if profile.get("sender_position"):
        lines.append(profile["sender_position"])
    if profile.get("sender_company"):
        lines.append(profile["sender_company"])
    if profile.get("sender_phone"):
        lines.append(f"тел. {profile['sender_phone']}")
    if profile.get("sender_website"):
        lines.append(profile["sender_website"])
    if profile.get("signature_note"):
        lines.append(profile["signature_note"])
    lines.extend(["", "---", f"Ответьте на это письмо, пожалуйста оставьте в теме код {token_mark}."])
    return "\n".join(lines)


def draft_opening_message(conn, chat_id: int) -> str:
    from app.services import get_manager_profile

    chat = get_chat(conn, chat_id)
    if not chat:
        raise ValueError("Чат не найден")
    profile = get_manager_profile(conn)
    return build_opening_text(
        supplier_name=chat["supplier_name"],
        contact=chat.get("contact_person") or "",
        items=chat["line_items"],
        profile=profile,
        token_mark=f"[CHAT-{chat['token']}]",
        conditions=chat.get("letter_conditions") or "",
    )


def ai_prepare_first_letter(
    conn,
    *,
    supplier_name: str,
    contact: str,
    items: list[dict[str, Any]],
    conditions: str = "",
    extra_hint: str = "",
) -> dict[str, str]:
    """Return editable subject+body for first chat letter (AI polish or template)."""
    from app.config import get_settings
    from app.services import get_manager_profile

    profile = get_manager_profile(conn)
    token_placeholder = "[CHAT-XXXX]"
    base_body = build_opening_text(
        supplier_name=supplier_name,
        contact=contact,
        items=items,
        profile=profile,
        token_mark=token_placeholder,
        conditions=conditions,
    )
    base_subject = f"Запрос по позициям {token_placeholder}"

    if not get_settings().ai_ready:
        return {"subject": base_subject, "body": base_body, "source": "template"}

    try:
        from app.supplier_search import ai_draft_rfq_letter

        drafted = ai_draft_rfq_letter(
            supplier_name=supplier_name,
            contact=contact,
            items=[
                {
                    "name": (
                        it.get("custom_name") or it.get("name") or it.get("product_name") or ""
                    ).strip(),
                    "quantity": it.get("quantity") or 1,
                    "unit": it.get("unit") or "шт",
                    "note": it.get("note") or "",
                }
                for it in items
            ],
            sender_name=profile["sender_name"],
            company=profile["sender_company"],
            token_mark=token_placeholder,
            extra_instruction=extra_hint
            or "Это переписка в чате с постоянным поставщиком, тон деловой и короткий.",
            letter_conditions=conditions,
            base_subject=base_subject,
            base_body=base_body,
            position=profile.get("sender_position") or "",
            phone=profile.get("sender_phone") or "",
            website=profile.get("sender_website") or "",
            signature_note=profile.get("signature_note") or "",
        )
        return {
            "subject": drafted.get("subject") or base_subject,
            "body": drafted.get("body") or base_body,
            "source": "ai",
        }
    except Exception:
        return {"subject": base_subject, "body": base_body, "source": "template"}


def build_chat_analytics(conn, chat: dict[str, Any]) -> dict[str, Any]:
    """Price/conditions summary from inbound chat messages (like RFQ matrix)."""
    line_items = chat.get("line_items") or []
    inbound = [m for m in (chat.get("messages") or []) if m.get("direction") == "in"]
    suppliers_block = []
    price_series = []
    has_comparison = False

    for m in reversed(inbound):  # newest first for "latest"
        lines = rows_to_dicts(
            conn.execute(
                "SELECT * FROM chat_quote_lines WHERE chat_message_id=? ORDER BY id",
                (m["id"],),
            ).fetchall()
        )
        by_item: dict[int, dict] = {}
        for line in lines:
            cid = line.get("chat_item_id")
            if cid:
                by_item[int(cid)] = line
        comparison = m.get("comparison")
        if comparison or m.get("comparison_summary"):
            has_comparison = True
        quotes = []
        for it in line_items:
            q = by_item.get(int(it["id"]))
            quotes.append({"item": it, "quote": q})
            if q and q.get("unit_price") is not None:
                price_series.append(
                    {
                        "product": _item_display_name(it),
                        "price": q["unit_price"],
                        "lead_time_days": q.get("lead_time_days"),
                        "at": m.get("created_at") or "",
                    }
                )
        suppliers_block.append(
            {
                "message": m,
                "lines": lines,
                "quotes": quotes,
                "comparison": comparison,
                "summary": m.get("comparison_summary") or "",
                "files": m.get("files") or [],
            }
        )
        break  # latest inbound analytics primary

    # all inbound with comparisons for history
    history = []
    for m in inbound:
        if m.get("comparison") or m.get("comparison_summary") or m.get("files"):
            history.append(
                {
                    "at": m.get("created_at") or "",
                    "summary": m.get("comparison_summary") or "",
                    "comparison": m.get("comparison"),
                    "from_addr": m.get("from_addr") or "",
                    "files": m.get("files") or [],
                    "message_id": m["id"],
                }
            )

    return {
        "latest": suppliers_block[0] if suppliers_block else None,
        "history": history,
        "chart": price_series,
        "has_comparison": has_comparison or bool(price_series),
        "line_items": line_items,
        "conditions": chat.get("letter_conditions") or "",
    }


def send_chat_message(
    conn,
    *,
    chat_id: int,
    body: str,
    subject: str = "",
    to_email: str = "",
    remember_email: bool = True,
    attachment_uploads: list[tuple[str, bytes, str]] | None = None,
    company_account_id: int | None = None,
) -> dict[str, Any]:
    from app.services import get_manager_profile

    chat = get_chat(conn, chat_id)
    if not chat:
        raise ValueError("Чат не найден")
    text = (body or "").strip()
    if company_account_id:
        acc = get_company_account(conn, int(company_account_id))
        if acc:
            block = format_company_requisites(acc)
            if block not in text:
                text = (text + "\n\n" + block).strip() if text else block
    if not text and not (attachment_uploads or []):
        raise ValueError("Пустое сообщение")

    catalog = (chat.get("supplier_email") or "").strip()
    stored = (chat.get("reply_email") or "").strip()
    chosen = (to_email or "").strip() or stored or catalog
    if not chosen or "@" not in chosen:
        raise ValueError("Укажите email получателя")

    if remember_email and chosen.lower() != catalog.lower():
        conn.execute(
            "UPDATE supplier_chats SET reply_email=?, updated_at=? WHERE id=?",
            (chosen, _now(), chat_id),
        )
    elif remember_email and chosen.lower() == catalog.lower() and stored:
        conn.execute(
            "UPDATE supplier_chats SET reply_email='', updated_at=? WHERE id=?",
            (_now(), chat_id),
        )

    token_mark = f"[CHAT-{chat['token']}]"
    subj = (subject or "").strip() or f"Переписка {token_mark}"
    if token_mark not in subj:
        subj = f"{subj} {token_mark}".strip()

    profile = get_manager_profile(conn)
    cur = conn.execute(
        """
        INSERT INTO supplier_chat_messages
          (chat_id, direction, kind, subject, body, to_addr, status, created_at)
        VALUES (?, 'out', 'email', ?, ?, ?, 'pending', ?)
        """,
        (chat_id, subj, text, chosen, _now()),
    )
    mid = int(cur.lastrowid)
    for filename, data, mime in attachment_uploads or []:
        save_upload(
            conn,
            owner_type="chat_message",
            owner_id=mid,
            filename=filename,
            data=data,
            mime=mime,
        )
    payloads = file_payloads_for_send(conn, "chat_message", mid)
    try:
        msgid = send_email(
            to_addr=chosen,
            subject=subj,
            body=text or "(см. вложения)",
            from_name=profile["sender_name"],
            attachments=payloads or None,
        )
        conn.execute(
            """
            UPDATE supplier_chat_messages
            SET status='sent', message_id=?, sent_at=?, error=NULL
            WHERE id=?
            """,
            (msgid, _now(), mid),
        )
        conn.execute(
            "UPDATE supplier_chats SET updated_at=? WHERE id=?",
            (_now(), chat_id),
        )
        return {"ok": True, "message_id": mid, "to_addr": chosen}
    except Exception as exc:
        conn.execute(
            "UPDATE supplier_chat_messages SET status='error', error=? WHERE id=?",
            (str(exc), mid),
        )
        raise


def import_chat_inbound(
    conn,
    *,
    chat_id: int,
    body: str,
    channel: str = "manual",
    from_label: str = "",
    attachment_uploads: list[tuple[str, bytes, str]] | None = None,
    parse: bool = True,
) -> int:
    chat = get_chat(conn, chat_id)
    if not chat:
        raise ValueError("Чат не найден")
    text = (body or "").strip()
    if not text and not (attachment_uploads or []):
        raise ValueError("Пустой ответ")
    ch = (channel or "manual").strip().lower()
    mid_key = f"paste-{secrets.token_hex(8)}"
    cur = conn.execute(
        """
        INSERT INTO supplier_chat_messages
          (chat_id, direction, kind, subject, body, message_id, from_addr,
           source_channel, status, created_at, is_read)
        VALUES (?, 'in', 'import', ?, ?, ?, ?, ?, 'received', ?, 0)
        """,
        (
            chat_id,
            f"Ответ ({ch})",
            text or "(файлы без текста)",
            mid_key,
            from_label or chat.get("supplier_name") or "",
            ch,
            _now(),
        ),
    )
    msg_id = int(cur.lastrowid)
    for filename, data, mime in attachment_uploads or []:
        save_upload(
            conn,
            owner_type="chat_message",
            owner_id=msg_id,
            filename=filename,
            data=data,
            mime=mime,
        )
    conn.execute(
        "UPDATE supplier_chats SET updated_at=? WHERE id=?",
        (_now(), chat_id),
    )
    if parse and text:
        try:
            from app.parse_quotes import parse_and_store_chat_message

            parse_and_store_chat_message(conn, msg_id)
        except Exception:
            pass
    return msg_id


def set_file_kind(conn, file_id: int, kind: str) -> None:
    kind = (kind or "other").strip().lower()
    if kind not in ("kp", "invoice", "pdf", "image", "other"):
        kind = "other"
    conn.execute("UPDATE message_files SET kind=? WHERE id=?", (kind, file_id))


def delete_chat(conn, chat_id: int) -> None:
    row = conn.execute("SELECT id FROM supplier_chats WHERE id=?", (chat_id,)).fetchone()
    if not row:
        raise ValueError("Чат не найден")
    # files stay on disk orphaned — acceptable for MVP; cascade deletes DB rows
    msg_ids = [
        int(r["id"])
        for r in conn.execute(
            "SELECT id FROM supplier_chat_messages WHERE chat_id=?", (chat_id,)
        ).fetchall()
    ]
    if msg_ids:
        ph = ",".join("?" * len(msg_ids))
        conn.execute(
            f"DELETE FROM message_files WHERE owner_type='chat_message' AND owner_id IN ({ph})",
            msg_ids,
        )
    conn.execute("DELETE FROM supplier_chats WHERE id=?", (chat_id,))
