"""Business logic: catalog, RFQs, comparison."""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from typing import Any

from jinja2 import Environment, BaseLoader

from app.config import get_settings
from app.db import rows_to_dicts
from app.mail import send_email


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _token() -> str:
    return secrets.token_hex(4).upper()


def render_template(tpl: str, **ctx: Any) -> str:
    env = Environment(loader=BaseLoader(), autoescape=False)
    return env.from_string(tpl).render(**ctx)


def list_categories(conn) -> list[dict]:
    return rows_to_dicts(conn.execute("SELECT * FROM categories ORDER BY name").fetchall())


def list_products(conn, category_id: int | None = None) -> list[dict]:
    if category_id:
        rows = conn.execute(
            """
            SELECT p.*, c.name AS category_name
            FROM products p JOIN categories c ON c.id=p.category_id
            WHERE p.category_id=?
            ORDER BY p.name
            """,
            (category_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT p.*, c.name AS category_name
            FROM products p JOIN categories c ON c.id=p.category_id
            ORDER BY c.name, p.name
            """
        ).fetchall()
    return rows_to_dicts(rows)


def list_suppliers(conn) -> list[dict]:
    rows = rows_to_dicts(conn.execute("SELECT * FROM suppliers ORDER BY name").fetchall())
    for s in rows:
        links = conn.execute(
            """
            SELECT p.id, p.name, p.category_id, c.name AS category_name
            FROM product_suppliers ps
            JOIN products p ON p.id=ps.product_id
            JOIN categories c ON c.id=p.category_id
            WHERE ps.supplier_id=?
            ORDER BY c.name, p.name
            """,
            (s["id"],),
        ).fetchall()
        s["products"] = rows_to_dicts(links)
        s["product_ids"] = [p["id"] for p in s["products"]]
        s["category_ids"] = sorted({int(p["category_id"]) for p in s["products"] if p.get("category_id")})
        s["category_names"] = sorted({p["category_name"] for p in s["products"] if p.get("category_name")})
    return rows


def suppliers_for_products(conn, product_ids: list[int]) -> list[dict]:
    """Suppliers linked to the given products (exact product links)."""
    if not product_ids:
        return []
    placeholders = ",".join("?" * len(product_ids))
    rows = conn.execute(
        f"""
        SELECT DISTINCT s.*
        FROM suppliers s
        JOIN product_suppliers ps ON ps.supplier_id=s.id
        WHERE ps.product_id IN ({placeholders})
        ORDER BY s.name
        """,
        product_ids,
    ).fetchall()
    return rows_to_dicts(rows)


def suppliers_for_product_categories(conn, product_ids: list[int]) -> list[dict]:
    """Suppliers linked to any product in the same categories as the given products."""
    if not product_ids:
        return []
    placeholders = ",".join("?" * len(product_ids))
    cat_rows = conn.execute(
        f"SELECT DISTINCT category_id FROM products WHERE id IN ({placeholders})",
        product_ids,
    ).fetchall()
    cat_ids = [int(r["category_id"]) for r in cat_rows]
    if not cat_ids:
        return []
    cph = ",".join("?" * len(cat_ids))
    rows = conn.execute(
        f"""
        SELECT DISTINCT s.*
        FROM suppliers s
        JOIN product_suppliers ps ON ps.supplier_id=s.id
        JOIN products p ON p.id=ps.product_id
        WHERE p.category_id IN ({cph})
        ORDER BY s.name
        """,
        cat_ids,
    ).fetchall()
    out = rows_to_dicts(rows)
    by_id = {s["id"]: s for s in list_suppliers(conn)}
    return [by_id[s["id"]] for s in out if s["id"] in by_id]


def get_default_template(conn) -> dict:
    row = conn.execute(
        "SELECT * FROM email_templates WHERE is_default=1 ORDER BY id LIMIT 1"
    ).fetchone()
    if not row:
        row = conn.execute("SELECT * FROM email_templates ORDER BY id LIMIT 1").fetchone()
    return dict(row) if row else {}


def get_manager_profile(conn) -> dict[str, str]:
    """Signature used in letters; DB overrides .env defaults."""
    settings = get_settings()
    row = conn.execute("SELECT * FROM manager_profile WHERE id=1").fetchone()
    if not row:
        return {
            "sender_name": settings.sender_name or "Отдел закупок",
            "sender_company": settings.sender_company or "",
            "sender_position": "",
            "sender_phone": "",
            "sender_website": "",
            "signature_note": "",
        }
    keys = set(row.keys()) if hasattr(row, "keys") else set()
    return {
        "sender_name": (row["sender_name"] or settings.sender_name or "Отдел закупок").strip(),
        "sender_company": (row["sender_company"] or settings.sender_company or "").strip(),
        "sender_position": (row["sender_position"] or "").strip(),
        "sender_phone": (row["sender_phone"] or "").strip(),
        "sender_website": ((row["sender_website"] if "sender_website" in keys else "") or "").strip(),
        "signature_note": ((row["signature_note"] if "signature_note" in keys else "") or "").strip(),
    }


def save_manager_profile(
    conn,
    *,
    sender_name: str,
    sender_company: str,
    sender_position: str = "",
    sender_phone: str = "",
    sender_website: str = "",
    signature_note: str = "",
) -> None:
    website = (sender_website or "").strip()
    if website and not website.lower().startswith(("http://", "https://")):
        website = "https://" + website
    conn.execute(
        """
        INSERT INTO manager_profile
          (id, sender_name, sender_company, sender_position, sender_phone,
           sender_website, signature_note, updated_at)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          sender_name=excluded.sender_name,
          sender_company=excluded.sender_company,
          sender_position=excluded.sender_position,
          sender_phone=excluded.sender_phone,
          sender_website=excluded.sender_website,
          signature_note=excluded.signature_note,
          updated_at=excluded.updated_at
        """,
        (
            sender_name.strip() or "Отдел закупок",
            sender_company.strip(),
            sender_position.strip(),
            sender_phone.strip(),
            website,
            (signature_note or "").strip(),
            _now(),
        ),
    )


def create_rfq(
    conn,
    *,
    title: str,
    notes: str,
    items: list[dict[str, Any]],
    supplier_ids: list[int],
    letter_conditions: str = "",
) -> int:
    conditions = (letter_conditions or "").strip()
    cur = conn.execute(
        """
        INSERT INTO rfqs (title, status, notes, letter_conditions, created_at)
        VALUES (?, 'draft', ?, ?, ?)
        """,
        (title.strip() or "Запрос КП", notes.strip(), conditions, _now()),
    )
    rfq_id = int(cur.lastrowid)
    for it in items:
        conn.execute(
            """
            INSERT INTO rfq_items (rfq_id, product_id, quantity, unit, note)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                rfq_id,
                int(it["product_id"]),
                float(it.get("quantity") or 1),
                (it.get("unit") or "шт").strip(),
                (it.get("note") or "").strip(),
            ),
        )

    item_rows = conn.execute(
        """
        SELECT ri.*, p.name
        FROM rfq_items ri JOIN products p ON p.id=ri.product_id
        WHERE ri.rfq_id=?
        """,
        (rfq_id,),
    ).fetchall()
    item_ctx = [
        {"name": r["name"], "quantity": r["quantity"], "unit": r["unit"], "note": r["note"]}
        for r in item_rows
    ]

    for sid in supplier_ids:
        _draft_message_for_supplier(
            conn,
            rfq_id=rfq_id,
            supplier_id=int(sid),
            item_ctx=item_ctx,
            conditions=conditions,
        )
    return rfq_id


def _draft_message_for_supplier(
    conn,
    *,
    rfq_id: int,
    supplier_id: int,
    item_ctx: list[dict[str, Any]],
    conditions: str,
) -> int | None:
    """Create one pending rfq_message from template. Returns message id or None if exists/missing."""
    existing = conn.execute(
        "SELECT id FROM rfq_messages WHERE rfq_id=? AND supplier_id=?",
        (rfq_id, supplier_id),
    ).fetchone()
    if existing:
        return None
    supplier = conn.execute("SELECT * FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
    if not supplier:
        return None
    profile = get_manager_profile(conn)
    tpl = get_default_template(conn)
    token = _token()
    token_mark = f"[RFQ-{token}]"
    ctx = {
        "token": token_mark,
        "contact": supplier["contact_person"] or "",
        "sender_name": profile["sender_name"],
        "company": profile["sender_company"],
        "position": profile["sender_position"],
        "phone": profile["sender_phone"],
        "website": profile.get("sender_website") or "",
        "signature_note": profile.get("signature_note") or "",
        "items": item_ctx,
        "conditions": conditions,
        "reply_hint": True,
    }
    subject = render_template(tpl.get("subject_tpl") or "{{ token }}", **ctx)
    body = render_template(tpl.get("body_tpl") or "", **ctx)
    if conditions and conditions not in body:
        insert_at = body.find("Просьба указать")
        block = f"Дополнительные условия и требования по этому запросу:\n{conditions}\n\n"
        if insert_at >= 0:
            body = body[:insert_at] + block + body[insert_at:]
        else:
            body = body.rstrip() + "\n\n" + block
    if token_mark not in subject:
        subject = f"{subject} {token_mark}".strip()
    cur = conn.execute(
        """
        INSERT INTO rfq_messages (rfq_id, supplier_id, token, subject, body, status)
        VALUES (?, ?, ?, ?, ?, 'pending')
        """,
        (rfq_id, supplier_id, token, subject, body),
    )
    return int(cur.lastrowid)


def add_suppliers_to_rfq(
    conn,
    rfq_id: int,
    supplier_ids: list[int],
    *,
    use_ai: bool = False,
    ai_hint: str = "",
) -> dict[str, Any]:
    """Add new suppliers to an existing RFQ and draft first messages for them."""
    rfq = conn.execute("SELECT * FROM rfqs WHERE id=?", (rfq_id,)).fetchone()
    if not rfq:
        raise ValueError("Запрос не найден")
    item_rows = conn.execute(
        """
        SELECT ri.*, p.name, p.category_id
        FROM rfq_items ri JOIN products p ON p.id=ri.product_id
        WHERE ri.rfq_id=?
        """,
        (rfq_id,),
    ).fetchall()
    if not item_rows:
        raise ValueError("В запросе нет позиций")
    product_ids = [int(r["product_id"]) for r in item_rows]
    allowed = {s["id"] for s in suppliers_for_product_categories(conn, product_ids)}
    item_ctx = [
        {"name": r["name"], "quantity": r["quantity"], "unit": r["unit"], "note": r["note"]}
        for r in item_rows
    ]
    conditions = (rfq["letter_conditions"] if "letter_conditions" in rfq.keys() else "") or ""
    added = 0
    skipped = 0
    new_message_ids: list[int] = []
    for sid in supplier_ids:
        sid = int(sid)
        if sid not in allowed:
            skipped += 1
            continue
        mid = _draft_message_for_supplier(
            conn,
            rfq_id=rfq_id,
            supplier_id=sid,
            item_ctx=item_ctx,
            conditions=conditions,
        )
        if mid is None:
            skipped += 1
            continue
        new_message_ids.append(mid)
        added += 1

    ai_result = None
    if use_ai and new_message_ids:
        # polish only newly drafted letters
        combined = {"updated": 0, "total": 0, "errors": []}
        for mid in new_message_ids:
            part = ai_redraft_rfq(conn, rfq_id, extra_instruction=ai_hint, message_id=mid)
            combined["updated"] += int(part.get("updated") or 0)
            combined["total"] += int(part.get("total") or 0)
            combined["errors"].extend(part.get("errors") or [])
        ai_result = combined

    return {
        "added": added,
        "skipped": skipped,
        "message_ids": new_message_ids,
        "ai": ai_result,
    }


def send_rfq(conn, rfq_id: int) -> dict[str, Any]:
    messages = conn.execute(
        """
        SELECT m.*, s.email, s.name AS supplier_name
        FROM rfq_messages m
        JOIN suppliers s ON s.id=m.supplier_id
        WHERE m.rfq_id=? AND m.status='pending'
        """,
        (rfq_id,),
    ).fetchall()
    profile = get_manager_profile(conn)
    sent = 0
    errors: list[str] = []
    for m in messages:
        try:
            mid = send_email(
                to_addr=m["email"],
                subject=m["subject"],
                body=m["body"],
                from_name=profile["sender_name"],
            )
            conn.execute(
                """
                UPDATE rfq_messages
                SET status='sent', message_id=?, sent_at=?, error=NULL
                WHERE id=?
                """,
                (mid, _now(), m["id"]),
            )
            sent += 1
        except Exception as exc:
            conn.execute(
                "UPDATE rfq_messages SET status='error', error=? WHERE id=?",
                (str(exc), m["id"]),
            )
            errors.append(f"{m['supplier_name']}: {exc}")
    if sent:
        conn.execute(
            "UPDATE rfqs SET status='sent', sent_at=? WHERE id=?",
            (_now(), rfq_id),
        )
    return {"sent": sent, "errors": errors, "total": len(messages)}


def rfq_detail(conn, rfq_id: int) -> dict[str, Any] | None:
    rfq = conn.execute("SELECT * FROM rfqs WHERE id=?", (rfq_id,)).fetchone()
    if not rfq:
        return None
    data = dict(rfq)
    # 'items' overlaps dict.items — keep both keys for templates/API
    line_items = rows_to_dicts(
        conn.execute(
            """
            SELECT ri.*, p.name AS product_name, c.name AS category_name
            FROM rfq_items ri
            JOIN products p ON p.id=ri.product_id
            JOIN categories c ON c.id=p.category_id
            WHERE ri.rfq_id=?
            """,
            (rfq_id,),
        ).fetchall()
    )
    data["line_items"] = line_items
    data["items"] = line_items
    data["messages"] = rows_to_dicts(
        conn.execute(
            """
            SELECT m.*, s.name AS supplier_name, s.email AS supplier_email,
                   (SELECT COUNT(*) FROM quote_replies qr WHERE qr.rfq_message_id=m.id) AS replies
            FROM rfq_messages m
            JOIN suppliers s ON s.id=m.supplier_id
            WHERE m.rfq_id=?
            ORDER BY s.name
            """,
            (rfq_id,),
        ).fetchall()
    )
    return data


def _parse_comparison_blob(reply: dict[str, Any]) -> dict[str, Any] | None:
    raw = (reply.get("comparison_json") or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def comparison_matrix(conn, rfq_id: int) -> dict[str, Any]:
    """Build price/lead-time matrix for charts and table."""
    detail = rfq_detail(conn, rfq_id)
    if not detail:
        return {}

    from app.parse_quotes import strip_mail_noise

    items = detail.get("line_items") or detail.get("items") or []
    suppliers = []
    for m in detail["messages"]:
        replies = rows_to_dicts(
            conn.execute(
                """
                SELECT * FROM quote_replies
                WHERE rfq_message_id=?
                ORDER BY received_at DESC
                """,
                (m["id"],),
            ).fetchall()
        )
        for r in replies:
            r["display_body"] = strip_mail_noise(r.get("raw_body") or "")
            r["comparison"] = _parse_comparison_blob(r)

        latest_lines: list[dict] = []
        if replies:
            latest_lines = rows_to_dicts(
                conn.execute(
                    "SELECT * FROM quote_lines WHERE quote_reply_id=? ORDER BY id",
                    (replies[0]["id"],),
                ).fetchall()
            )

        by_item: dict[int, dict] = {}
        for line in latest_lines:
            rid = line.get("rfq_item_id")
            if rid:
                by_item[int(rid)] = line

        name_map = {it["product_name"].lower(): it["id"] for it in items}
        for line in latest_lines:
            if line.get("rfq_item_id"):
                continue
            pname = (line.get("product_name") or "").lower()
            for n, iid in name_map.items():
                if n and (n in pname or pname in n):
                    by_item[iid] = line
                    break

        suppliers.append(
            {
                "supplier_id": m["supplier_id"],
                "supplier_name": m["supplier_name"],
                "status": m["status"],
                "replies_count": m["replies"],
                "by_item": by_item,
                "raw_replies": replies,
                "latest_comparison": replies[0].get("comparison") if replies else None,
                "latest_summary": (replies[0].get("comparison_summary") if replies else "") or "",
                "message": m,
            }
        )

    threads = _build_threads(conn, detail, suppliers)

    rows = []
    price_series = []
    for it in items:
        cell = {"item": it, "quotes": []}
        for s in suppliers:
            q = s["by_item"].get(it["id"])
            if not q:
                for v in s["by_item"].values():
                    if (v.get("product_name") or "").lower() == it["product_name"].lower():
                        q = v
                        break
            cell["quotes"].append({"supplier": s["supplier_name"], "quote": q})
            if q and q.get("unit_price") is not None:
                price_series.append(
                    {
                        "product": it["product_name"],
                        "supplier": s["supplier_name"],
                        "price": q["unit_price"],
                        "lead_time_days": q.get("lead_time_days"),
                    }
                )
        rows.append(cell)

    product_ids = [int(it["product_id"]) for it in items if it.get("product_id")]
    already = {int(m["supplier_id"]) for m in detail["messages"]}
    eligible = suppliers_for_product_categories(conn, product_ids)
    candidates = [s for s in eligible if int(s["id"]) not in already]
    category_names = sorted(
        {
            (it.get("category_name") or "").strip()
            for it in items
            if (it.get("category_name") or "").strip()
        }
    )

    return {
        "rfq": detail,
        "rows": rows,
        "suppliers": suppliers,
        "threads": threads,
        "draft_messages": [
            m for m in detail["messages"] if m.get("status") in ("pending", "error")
        ],
        "candidate_suppliers": candidates,
        "eligible_supplier_count": len(eligible),
        "rfq_category_names": category_names,
        "chart": price_series,
        "has_comparison": any(
            (s.get("latest_summary") or s.get("latest_comparison")) for s in suppliers
        ),
    }


def _build_threads(conn, detail: dict[str, Any], suppliers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from app.files import kind_label, list_files_for_owners
    from app.parse_quotes import strip_mail_noise

    threads: list[dict[str, Any]] = []
    followup_ids: list[int] = []
    reply_ids: list[int] = []
    for s in suppliers:
        m = s.get("message") or {}
        for f in rows_to_dicts(
            conn.execute(
                "SELECT id FROM rfq_followups WHERE rfq_message_id=?",
                (m.get("id"),),
            ).fetchall()
        ):
            followup_ids.append(int(f["id"]))
        for r in s.get("raw_replies") or []:
            if r.get("id"):
                reply_ids.append(int(r["id"]))
    followup_files = list_files_for_owners(conn, "rfq_followup", followup_ids)
    reply_files = list_files_for_owners(conn, "quote_reply", reply_ids)

    for s in suppliers:
        m = s.get("message") or {}
        events: list[dict[str, Any]] = []
        # initial outgoing RFQ (only if already sent / has body worth showing)
        if m.get("status") in ("sent", "replied", "collecting") or (
            m.get("status") not in ("pending", "error") and m.get("sent_at")
        ):
            events.append(
                {
                    "direction": "out",
                    "kind": "rfq",
                    "at": m.get("sent_at") or detail.get("sent_at") or m.get("created_at") or "",
                    "subject": m.get("subject") or "",
                    "body": m.get("body") or "",
                    "parse_status": "",
                    "comparison_summary": "",
                    "reply_id": None,
                    "files": [],
                }
            )
        elif m.get("status") in ("pending", "error"):
            events.append(
                {
                    "direction": "out",
                    "kind": "draft",
                    "at": detail.get("created_at") or "",
                    "subject": m.get("subject") or "",
                    "body": m.get("body") or "",
                    "parse_status": "",
                    "comparison_summary": "",
                    "reply_id": None,
                    "files": [],
                }
            )

        followups = rows_to_dicts(
            conn.execute(
                """
                SELECT * FROM rfq_followups
                WHERE rfq_message_id=?
                ORDER BY created_at ASC, id ASC
                """,
                (m.get("id"),),
            ).fetchall()
        )
        for f in followups:
            if f.get("status") == "pending":
                continue
            files = followup_files.get(int(f["id"]), [])
            for fl in files:
                fl["kind_label"] = kind_label(fl.get("kind") or "")
            events.append(
                {
                    "direction": "out",
                    "kind": "followup",
                    "at": f.get("sent_at") or f.get("created_at") or "",
                    "subject": f.get("subject") or "",
                    "body": f.get("body") or "",
                    "parse_status": f.get("status") or "",
                    "comparison_summary": "",
                    "reply_id": None,
                    "to_addr": (f.get("to_addr") or "").strip(),
                    "files": files,
                }
            )

        last_from_addr = ""
        for r in reversed(s.get("raw_replies") or []):
            ch = (r.get("source_channel") or "email").strip().lower()
            from_addr = (r.get("from_addr") or "").strip()
            if ch == "email" and from_addr and "@" in from_addr:
                last_from_addr = from_addr
            files = reply_files.get(int(r["id"]), []) if r.get("id") else []
            for fl in files:
                fl["kind_label"] = kind_label(fl.get("kind") or "")
            events.append(
                {
                    "direction": "in",
                    "kind": "reply",
                    "channel": ch,
                    "at": r.get("received_at") or "",
                    "subject": r.get("subject") or "",
                    "body": r.get("display_body") or strip_mail_noise(r.get("raw_body") or ""),
                    "parse_status": r.get("parse_status") or "",
                    "comparison_summary": r.get("comparison_summary") or "",
                    "reply_id": r.get("id"),
                    "from_addr": from_addr if ch == "email" else "",
                    "files": files,
                    "is_new": not bool(r.get("is_read", 1)),
                }
            )

        catalog_email = (m.get("supplier_email") or "").strip()
        reply_email = (m.get("reply_email") or "").strip()
        active_email = reply_email or catalog_email
        from_mismatch = bool(
            last_from_addr
            and catalog_email
            and last_from_addr.lower() != catalog_email.lower()
        )
        unread_count = sum(
            1 for r in (s.get("raw_replies") or []) if not bool(r.get("is_read", 1))
        )

        events.sort(key=lambda e: e.get("at") or "")
        threads.append(
            {
                "message_id": m.get("id"),
                "supplier_id": s.get("supplier_id"),
                "supplier_name": s.get("supplier_name"),
                "supplier_email": catalog_email,
                "catalog_email": catalog_email,
                "reply_email": reply_email,
                "active_email": active_email,
                "last_from_addr": last_from_addr,
                "from_mismatch": from_mismatch,
                "token": m.get("token") or "",
                "status": m.get("status") or "",
                "events": events,
                "latest_summary": s.get("latest_summary") or "",
                "unread_hint": unread_count > 0,
                "unread_count": unread_count,
                "can_followup": bool(m.get("sent_at"))
                or m.get("status") in ("sent", "replied", "collecting"),
            }
        )
    return threads


def mark_rfq_replies_read(conn, rfq_id: int) -> int:
    cur = conn.execute(
        """
        UPDATE quote_replies
        SET is_read=1
        WHERE is_read=0 AND rfq_message_id IN (
          SELECT id FROM rfq_messages WHERE rfq_id=?
        )
        """,
        (rfq_id,),
    )
    return int(cur.rowcount or 0)


def count_unread_rfq_replies(conn, rfq_id: int | None = None) -> int:
    if rfq_id:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM quote_replies qr
            JOIN rfq_messages m ON m.id=qr.rfq_message_id
            WHERE qr.is_read=0 AND m.rfq_id=?
            """,
            (rfq_id,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM quote_replies WHERE is_read=0"
        ).fetchone()
    return int(row["c"] if row else 0)


def set_thread_reply_email(
    conn,
    *,
    rfq_id: int,
    message_id: int,
    email: str,
    update_supplier: bool = False,
) -> dict[str, Any]:
    """Set per-thread reply address; optionally update supplier card email."""
    email = (email or "").strip()
    if not email or "@" not in email:
        raise ValueError("Укажите корректный email")
    row = conn.execute(
        """
        SELECT m.id, m.supplier_id, s.email AS catalog_email, s.name AS supplier_name
        FROM rfq_messages m
        JOIN suppliers s ON s.id=m.supplier_id
        WHERE m.id=? AND m.rfq_id=?
        """,
        (message_id, rfq_id),
    ).fetchone()
    if not row:
        raise ValueError("Тред не найден")
    conn.execute(
        "UPDATE rfq_messages SET reply_email=? WHERE id=?",
        (email, message_id),
    )
    supplier_updated = False
    if update_supplier:
        conn.execute("UPDATE suppliers SET email=? WHERE id=?", (email, row["supplier_id"]))
        supplier_updated = True
    return {
        "email": email,
        "supplier_updated": supplier_updated,
        "supplier_name": row["supplier_name"],
        "catalog_email": row["catalog_email"],
    }


def rename_rfq(conn, rfq_id: int, title: str) -> None:
    title = (title or "").strip()
    if not title:
        raise ValueError("Название не может быть пустым")
    row = conn.execute("SELECT id FROM rfqs WHERE id=?", (rfq_id,)).fetchone()
    if not row:
        raise ValueError("Запрос не найден")
    conn.execute("UPDATE rfqs SET title=? WHERE id=?", (title, rfq_id))


def delete_rfq(conn, rfq_id: int) -> None:
    row = conn.execute("SELECT id FROM rfqs WHERE id=?", (rfq_id,)).fetchone()
    if not row:
        raise ValueError("Запрос не найден")
    # Cascades: messages → replies/lines/followups via FK
    conn.execute("DELETE FROM rfqs WHERE id=?", (rfq_id,))


_RU_MONTHS = (
    "",
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)


def format_created_month_year(iso: str | None) -> str:
    """'2026-08-10T...' → 'август 2026'."""
    if not iso:
        return "—"
    try:
        raw = (iso or "")[:10]
        y, m, _d = raw.split("-")
        month = _RU_MONTHS[int(m)]
        return f"{month} {y}"
    except Exception:
        return (iso or "")[:7] or "—"


def update_rfq_message(conn, message_id: int, *, subject: str, body: str) -> None:
    row = conn.execute("SELECT id, status, token FROM rfq_messages WHERE id=?", (message_id,)).fetchone()
    if not row:
        raise ValueError("Письмо не найдено")
    if row["status"] not in ("pending", "error"):
        raise ValueError("Можно править только неотправленные письма")
    token_mark = f"[RFQ-{row['token']}]"
    subject = (subject or "").strip()
    body = (body or "").strip()
    if token_mark not in subject:
        subject = f"{subject} {token_mark}".strip()
    conn.execute(
        "UPDATE rfq_messages SET subject=?, body=?, status='pending', error=NULL WHERE id=?",
        (subject, body, message_id),
    )


def ai_redraft_rfq(conn, rfq_id: int, *, extra_instruction: str = "", message_id: int | None = None) -> dict[str, Any]:
    """Polish template drafts with AI; keep manager conditions as facts."""
    from app.supplier_search import ai_draft_rfq_letter

    profile = get_manager_profile(conn)
    rfq = conn.execute("SELECT * FROM rfqs WHERE id=?", (rfq_id,)).fetchone()
    letter_conditions = ""
    if rfq:
        letter_conditions = (rfq["letter_conditions"] if "letter_conditions" in rfq.keys() else "") or ""
    detail_items = conn.execute(
        """
        SELECT ri.*, p.name
        FROM rfq_items ri JOIN products p ON p.id=ri.product_id
        WHERE ri.rfq_id=?
        """,
        (rfq_id,),
    ).fetchall()
    items = [
        {"name": r["name"], "quantity": r["quantity"], "unit": r["unit"], "note": r["note"]}
        for r in detail_items
    ]
    if message_id:
        messages = conn.execute(
            """
            SELECT m.*, s.name AS supplier_name, s.contact_person
            FROM rfq_messages m JOIN suppliers s ON s.id=m.supplier_id
            WHERE m.rfq_id=? AND m.id=? AND m.status IN ('pending','error')
            """,
            (rfq_id, message_id),
        ).fetchall()
    else:
        messages = conn.execute(
            """
            SELECT m.*, s.name AS supplier_name, s.contact_person
            FROM rfq_messages m JOIN suppliers s ON s.id=m.supplier_id
            WHERE m.rfq_id=? AND m.status IN ('pending','error')
            """,
            (rfq_id,),
        ).fetchall()

    updated = 0
    errors: list[str] = []
    for m in messages:
        token_mark = f"[RFQ-{m['token']}]"
        try:
            draft = ai_draft_rfq_letter(
                supplier_name=m["supplier_name"],
                contact=m["contact_person"] or "",
                items=items,
                sender_name=profile["sender_name"],
                company=profile["sender_company"],
                token_mark=token_mark,
                extra_instruction=extra_instruction,
                letter_conditions=letter_conditions,
                base_subject=m["subject"] or "",
                base_body=m["body"] or "",
                position=profile.get("sender_position") or "",
                phone=profile.get("sender_phone") or "",
                website=profile.get("sender_website") or "",
                signature_note=profile.get("signature_note") or "",
            )
            conn.execute(
                "UPDATE rfq_messages SET subject=?, body=?, status='pending', error=NULL WHERE id=?",
                (draft["subject"], draft["body"], m["id"]),
            )
            updated += 1
        except Exception as exc:
            errors.append(f"{m['supplier_name']}: {exc}")
    return {"updated": updated, "errors": errors, "total": len(messages)}


def send_followup(
    conn,
    *,
    rfq_id: int,
    message_id: int,
    body: str,
    use_ai: bool = False,
    to_email: str = "",
    remember: bool = True,
    attachment_uploads: list[tuple[str, bytes, str]] | None = None,
    company_account_id: int | None = None,
) -> dict[str, Any]:
    """Send a clarification email in the supplier thread (keeps RFQ token)."""
    from app.chats import format_company_requisites, get_company_account
    from app.files import file_payloads_for_send, save_upload
    from app.supplier_search import ai_draft_followup

    row = conn.execute(
        """
        SELECT m.*, s.email AS catalog_email, s.name AS supplier_name, s.contact_person
        FROM rfq_messages m
        JOIN suppliers s ON s.id=m.supplier_id
        WHERE m.id=? AND m.rfq_id=?
        """,
        (message_id, rfq_id),
    ).fetchone()
    if not row:
        raise ValueError("Тред не найден")
    if not (row["sent_at"] or row["status"] in ("sent", "replied", "collecting")):
        raise ValueError("Сначала отправьте исходный запрос этому поставщику")

    text = (body or "").strip()
    if company_account_id:
        acc = get_company_account(conn, int(company_account_id))
        if acc:
            block = format_company_requisites(acc)
            if block not in text:
                text = (text + "\n\n" + block).strip() if text else block
    if not text and not (attachment_uploads or []):
        raise ValueError("Пустое уточнение")

    catalog = (row["catalog_email"] or "").strip()
    stored_reply = (row["reply_email"] or "").strip() if "reply_email" in row.keys() else ""
    chosen = (to_email or "").strip() or stored_reply or catalog
    if not chosen or "@" not in chosen:
        raise ValueError(
            "Нет адреса для ответа — укажите email в форме или в карточке поставщика"
        )

    if remember:
        if chosen.lower() != catalog.lower():
            conn.execute(
                "UPDATE rfq_messages SET reply_email=? WHERE id=?",
                (chosen, message_id),
            )
        elif stored_reply:
            conn.execute(
                "UPDATE rfq_messages SET reply_email='' WHERE id=?",
                (message_id,),
            )

    token_mark = f"[RFQ-{row['token']}]"
    subject = f"Уточнение по запросу {token_mark}"
    profile = get_manager_profile(conn)
    rfq = conn.execute("SELECT * FROM rfqs WHERE id=?", (rfq_id,)).fetchone()
    conditions = ""
    if rfq and "letter_conditions" in rfq.keys():
        conditions = rfq["letter_conditions"] or ""

    if use_ai and text:
        drafted = ai_draft_followup(
            supplier_name=row["supplier_name"],
            contact=row["contact_person"] or "",
            notes=text,
            sender_name=profile["sender_name"],
            company=profile["sender_company"],
            position=profile.get("sender_position") or "",
            phone=profile.get("sender_phone") or "",
            website=profile.get("sender_website") or "",
            signature_note=profile.get("signature_note") or "",
            token_mark=token_mark,
            letter_conditions=conditions,
            prior_subject=row["subject"] or "",
        )
        subject = drafted["subject"]
        text = drafted["body"]

    if token_mark not in subject:
        subject = f"{subject} {token_mark}".strip()

    cur = conn.execute(
        """
        INSERT INTO rfq_followups
          (rfq_id, rfq_message_id, supplier_id, subject, body, status, created_at, to_addr)
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (rfq_id, message_id, row["supplier_id"], subject, text or "(см. вложения)", _now(), chosen),
    )
    fid = int(cur.lastrowid)
    for filename, data, mime in attachment_uploads or []:
        save_upload(
            conn,
            owner_type="rfq_followup",
            owner_id=fid,
            filename=filename,
            data=data,
            mime=mime,
        )
    payloads = file_payloads_for_send(conn, "rfq_followup", fid)
    try:
        mid = send_email(
            to_addr=chosen,
            subject=subject,
            body=text or "(см. вложения)",
            from_name=profile["sender_name"],
            attachments=payloads or None,
        )
        conn.execute(
            """
            UPDATE rfq_followups
            SET status='sent', message_id=?, sent_at=?, error=NULL, to_addr=?
            WHERE id=?
            """,
            (mid, _now(), chosen, fid),
        )
        return {"ok": True, "followup_id": fid, "to_addr": chosen}
    except Exception as exc:
        conn.execute(
            "UPDATE rfq_followups SET status='error', error=? WHERE id=?",
            (str(exc), fid),
        )
        raise


def sync_conditions_from_thread(conn, *, rfq_id: int, message_id: int) -> dict[str, Any]:
    """AI: refresh letter_conditions from the supplier correspondence."""
    from app.ai_client import AiError, chat_json
    from app.parse_quotes import strip_mail_noise

    rfq = conn.execute("SELECT * FROM rfqs WHERE id=?", (rfq_id,)).fetchone()
    if not rfq:
        raise ValueError("Запрос не найден")
    msg = conn.execute(
        """
        SELECT m.*, s.name AS supplier_name
        FROM rfq_messages m JOIN suppliers s ON s.id=m.supplier_id
        WHERE m.id=? AND m.rfq_id=?
        """,
        (message_id, rfq_id),
    ).fetchone()
    if not msg:
        raise ValueError("Тред не найден")

    replies = rows_to_dicts(
        conn.execute(
            """
            SELECT subject, raw_body, comparison_summary, received_at
            FROM quote_replies WHERE rfq_message_id=?
            ORDER BY received_at ASC
            """,
            (message_id,),
        ).fetchall()
    )
    followups = rows_to_dicts(
        conn.execute(
            """
            SELECT subject, body, sent_at, status FROM rfq_followups
            WHERE rfq_message_id=? AND status='sent'
            ORDER BY sent_at ASC
            """,
            (message_id,),
        ).fetchall()
    )
    parts = [
        f"Исходные условия запроса:\n{(rfq['letter_conditions'] or '—')}",
        f"\nНаше исходное письмо:\n{msg['body'][:4000]}",
    ]
    for f in followups:
        parts.append(f"\nНаше уточнение ({f.get('sent_at')}):\n{f.get('body')}")
    for r in replies:
        parts.append(
            f"\nОтвет поставщика ({r.get('received_at')}):\n"
            f"{strip_mail_noise(r.get('raw_body') or '')[:4000]}"
            + (f"\nСводка разбора: {r['comparison_summary']}" if r.get("comparison_summary") else "")
        )
    data = chat_json(
        system=(
            "Ты помощник закупщика. По переписке с поставщиком обнови список условий запроса. "
            "Верни JSON {\"letter_conditions\":\"...\",\"summary\":\"кратко что изменилось\"}. "
            "letter_conditions — маркированный список актуальных требований/фактов "
            "(производитель, объём, цена если согласовали, доставка, Меркурий, оплата и т.д.). "
            "Не выдумывай то, чего нет в переписке. Без markdown-ограждений."
        ),
        user="\n".join(parts)[:14000],
        temperature=0.2,
        timeout=75.0,
    )
    if not isinstance(data, dict):
        raise AiError("Некорректный ответ AI")
    conditions = str(data.get("letter_conditions") or "").strip()
    summary = str(data.get("summary") or "").strip()
    if not conditions:
        raise AiError("AI не вернул условия")
    conn.execute(
        "UPDATE rfqs SET letter_conditions=? WHERE id=?",
        (conditions, rfq_id),
    )
    return {"letter_conditions": conditions, "summary": summary}


CHANNEL_LABELS = {
    "email": "почта",
    "telegram": "Telegram",
    "max": "Макс",
    "whatsapp": "WhatsApp",
    "manual": "вставка",
    "other": "другое",
}


def import_pasted_reply(
    conn,
    *,
    rfq_id: int,
    message_id: int,
    body: str,
    channel: str = "manual",
    update_conditions: bool = True,
) -> dict[str, Any]:
    """
    Paste a supplier answer from Telegram/Max/etc. into RFQ analytics.
    Same parse pipeline as email replies.
    """
    from app.parse_quotes import parse_and_store_reply

    text = (body or "").strip()
    if not text:
        raise ValueError("Вставьте текст ответа поставщика")

    msg = conn.execute(
        """
        SELECT m.*, s.email, s.name AS supplier_name
        FROM rfq_messages m
        JOIN suppliers s ON s.id=m.supplier_id
        WHERE m.id=? AND m.rfq_id=?
        """,
        (message_id, rfq_id),
    ).fetchone()
    if not msg:
        raise ValueError("Тред не найден")

    ch = (channel or "manual").strip().lower()
    if ch not in CHANNEL_LABELS:
        ch = "other"
    label = CHANNEL_LABELS[ch]
    mid = f"paste-{secrets.token_hex(8)}"
    token_mark = f"[RFQ-{msg['token']}]"
    subject = f"Ответ из {label} {token_mark}"

    cur = conn.execute(
        """
        INSERT INTO quote_replies
          (rfq_message_id, message_id, from_addr, subject, raw_body,
           parse_status, source_channel, received_at, is_read)
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, 0)
        """,
        (
            message_id,
            mid,
            f"{label}:{msg['supplier_name']}",
            subject,
            text,
            ch,
            _now(),
        ),
    )
    reply_id = int(cur.lastrowid)
    info = parse_and_store_reply(conn, reply_id)

    cond_info = None
    if update_conditions:
        try:
            cond_info = sync_conditions_from_thread(conn, rfq_id=rfq_id, message_id=message_id)
        except Exception as exc:
            cond_info = {"error": str(exc)}

    # mark RFQ as collecting if it was only sent
    conn.execute(
        "UPDATE rfqs SET status='collecting' WHERE id=? AND status IN ('sent','draft')",
        (rfq_id,),
    )
    return {
        "reply_id": reply_id,
        "lines": info.get("lines"),
        "source": info.get("source"),
        "channel": ch,
        "conditions": cond_info,
    }

