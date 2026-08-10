"""Parse supplier quote emails into structured quote_lines."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger("parse_quotes")

PRICE_RE = re.compile(
    r"(?P<name>.{3,80}?)\s*[—\-–:]\s*(?P<price>\d[\d\s]*([.,]\d{1,2})?)\s*(?P<cur>₽|руб\.?|RUB|USD|EUR)?",
    re.I,
)
LEAD_RE = re.compile(r"(\d+)\s*(раб\.?\s*)?(дн|дня|дней|day)", re.I)


def _heuristic_lines(body: str, products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    lower_body = body.lower()
    for p in products:
        name = (p.get("name") or "").strip()
        if not name:
            continue
        # find a window around product name
        idx = lower_body.find(name.lower())
        window = body[max(0, idx) : idx + 200] if idx >= 0 else body
        price = None
        currency = "RUB"
        m = PRICE_RE.search(window)
        if m:
            raw = m.group("price").replace(" ", "").replace(",", ".")
            try:
                price = float(raw)
            except ValueError:
                price = None
            cur = (m.group("cur") or "").lower()
            if "usd" in cur or "$" in cur:
                currency = "USD"
            elif "eur" in cur or "€" in cur:
                currency = "EUR"
        lead = None
        lm = LEAD_RE.search(window)
        if lm:
            lead = int(lm.group(1))
        in_stock = None
        if re.search(r"в наличии|на склад", window, re.I):
            in_stock = 1
        elif re.search(r"под заказ|нет в наличии", window, re.I):
            in_stock = 0
        if price is not None or lead is not None or in_stock is not None or idx >= 0:
            lines.append(
                {
                    "rfq_item_id": p.get("rfq_item_id"),
                    "product_name": name,
                    "unit_price": price,
                    "currency": currency,
                    "vat_included": None,
                    "lead_time_days": lead,
                    "in_stock": in_stock,
                    "notes": "",
                }
            )
    return lines


def _ai_lines(body: str, products: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    settings = get_settings()
    if not settings.ai_ready:
        return None

    catalog = [
        {"rfq_item_id": p["rfq_item_id"], "name": p["name"], "unit": p.get("unit"), "quantity": p.get("quantity")}
        for p in products
    ]
    system = (
        "Ты парсер коммерческих предложений поставщиков. "
        "Верни строго JSON-массив объектов с полями: "
        "rfq_item_id (число или null), product_name, unit_price (число|null), "
        "currency (RUB/USD/EUR), vat_included (true/false/null), "
        "lead_time_days (число|null), in_stock (true/false/null), notes (строка). "
        "Сопоставляй строки с позициями запроса по смыслу названия. "
        "Если данных нет — null. Без markdown."
    )
    user = f"Позиции запроса:\n{json.dumps(catalog, ensure_ascii=False)}\n\nПисьмо:\n{body[:12000]}"

    try:
        with httpx.Client(timeout=60.0) as client:
            r = client.post(
                f"{settings.ai_base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {settings.ai_api_key}"},
                json={
                    "model": settings.ai_model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.1,
                },
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
    except Exception:
        log.exception("AI parse failed")
        return None

    content = (content or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        log.warning("AI returned non-JSON: %s", content[:200])
        return None
    if not isinstance(data, list):
        return None

    out: list[dict[str, Any]] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        price = row.get("unit_price")
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None
        lead = row.get("lead_time_days")
        try:
            lead = int(lead) if lead is not None else None
        except (TypeError, ValueError):
            lead = None
        vat = row.get("vat_included")
        stock = row.get("in_stock")
        out.append(
            {
                "rfq_item_id": row.get("rfq_item_id"),
                "product_name": str(row.get("product_name") or ""),
                "unit_price": price,
                "currency": str(row.get("currency") or "RUB"),
                "vat_included": 1 if vat is True else (0 if vat is False else None),
                "lead_time_days": lead,
                "in_stock": 1 if stock is True else (0 if stock is False else None),
                "notes": str(row.get("notes") or ""),
            }
        )
    return out


def parse_and_store_reply(conn, reply_id: int) -> dict[str, Any]:
    reply = conn.execute("SELECT * FROM quote_replies WHERE id=?", (reply_id,)).fetchone()
    if not reply:
        raise ValueError("reply not found")

    msg = conn.execute(
        "SELECT * FROM rfq_messages WHERE id=?",
        (reply["rfq_message_id"],),
    ).fetchone()
    products = conn.execute(
        """
        SELECT ri.id AS rfq_item_id, p.name, ri.unit, ri.quantity
        FROM rfq_items ri
        JOIN products p ON p.id = ri.product_id
        WHERE ri.rfq_id = ?
        """,
        (msg["rfq_id"],),
    ).fetchall()
    product_dicts = [dict(p) for p in products]

    lines = _ai_lines(reply["raw_body"], product_dicts)
    source = "ai"
    if not lines:
        lines = _heuristic_lines(reply["raw_body"], product_dicts)
        source = "heuristic"

    conn.execute("DELETE FROM quote_lines WHERE quote_reply_id=?", (reply_id,))
    for line in lines:
        conn.execute(
            """
            INSERT INTO quote_lines
              (quote_reply_id, rfq_item_id, product_name, unit_price, currency,
               vat_included, lead_time_days, in_stock, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reply_id,
                line.get("rfq_item_id"),
                line.get("product_name") or "",
                line.get("unit_price"),
                line.get("currency") or "RUB",
                line.get("vat_included"),
                line.get("lead_time_days"),
                line.get("in_stock"),
                line.get("notes") or "",
            ),
        )
    conn.execute(
        "UPDATE quote_replies SET parse_status=?, parse_error=NULL WHERE id=?",
        (f"ok:{source}", reply_id),
    )
    return {"lines": len(lines), "source": source}
