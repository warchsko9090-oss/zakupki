"""Business logic: catalog, RFQs, comparison."""

from __future__ import annotations

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
            SELECT p.id, p.name, c.name AS category_name
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
    return rows


def suppliers_for_products(conn, product_ids: list[int]) -> list[dict]:
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


def get_default_template(conn) -> dict:
    row = conn.execute(
        "SELECT * FROM email_templates WHERE is_default=1 ORDER BY id LIMIT 1"
    ).fetchone()
    if not row:
        row = conn.execute("SELECT * FROM email_templates ORDER BY id LIMIT 1").fetchone()
    return dict(row) if row else {}


def create_rfq(
    conn,
    *,
    title: str,
    notes: str,
    items: list[dict[str, Any]],
    supplier_ids: list[int],
) -> int:
    cur = conn.execute(
        "INSERT INTO rfqs (title, status, notes, created_at) VALUES (?, 'draft', ?, ?)",
        (title.strip() or "Запрос КП", notes.strip(), _now()),
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

    settings = get_settings()
    tpl = get_default_template(conn)
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
        supplier = conn.execute("SELECT * FROM suppliers WHERE id=?", (sid,)).fetchone()
        if not supplier:
            continue
        token = _token()
        token_mark = f"[RFQ-{token}]"
        ctx = {
            "token": token_mark,
            "contact": supplier["contact_person"] or "",
            "sender_name": settings.sender_name,
            "company": settings.sender_company,
            "items": item_ctx,
            "reply_hint": True,
        }
        subject = render_template(tpl.get("subject_tpl") or "{{ token }}", **ctx)
        body = render_template(tpl.get("body_tpl") or "", **ctx)
        if token_mark not in subject:
            subject = f"{subject} {token_mark}".strip()
        conn.execute(
            """
            INSERT INTO rfq_messages (rfq_id, supplier_id, token, subject, body, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
            """,
            (rfq_id, sid, token, subject, body),
        )
    return rfq_id


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
    sent = 0
    errors: list[str] = []
    for m in messages:
        try:
            mid = send_email(to_addr=m["email"], subject=m["subject"], body=m["body"])
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
    data["items"] = rows_to_dicts(
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


def comparison_matrix(conn, rfq_id: int) -> dict[str, Any]:
    """Build price/lead-time matrix for charts and table."""
    detail = rfq_detail(conn, rfq_id)
    if not detail:
        return {}

    items = detail["items"]
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
            }
        )

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

    return {
        "rfq": detail,
        "rows": rows,
        "suppliers": suppliers,
        "chart": price_series,
    }
