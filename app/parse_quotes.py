"""Parse supplier quote emails into structured quote_lines + RFQ comparison."""

from __future__ import annotations

import html
import json
import logging
import re
from typing import Any

from app.config import get_settings

log = logging.getLogger("parse_quotes")

PRICE_RE = re.compile(
    r"(?:"
    r"(?:цен[аеуы]?\s*(?:за\s*(?:кг|шт|ед\.?|единицу)\s*)?[=:]?\s*)|"
    r"(?:по\s+цен[её]\s*)|"
    r"(?:стоим(?:ость|остью)\s*)"
    r")?"
    r"(?P<price>\d+(?:[.,]\d{1,2})?)\s*(?:₽|руб\.?|RUB|USD|EUR|\$|€)"
    r"(?:\s*(?:за|\/)\s*(?:кг|шт|л|т|ед\.?)?)?",
    re.I,
)
LEAD_RE = re.compile(r"(\d+)\s*(раб\.?\s*)?(дн|дня|дней|day)", re.I)
QTY_OFFER_RE = re.compile(
    r"(?:только|есть|в наличии|можем|доступно|осталось|предлагаем)\s*"
    r"(?:около\s*)?(\d+(?:[.,]\d+)?)\s*(кг|шт|л|т|упак)",
    re.I,
)
QTY_PLAIN_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(кг|шт|л|т)\b", re.I)


def strip_mail_noise(body: str) -> str:
    """Keep only the newest reply; drop quoted history, HTML and signatures."""
    text = body or ""
    if re.search(r"<[a-zA-Z][^>]*>", text):
        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</p\s*>", "\n", text)
        text = re.sub(r"(?i)</div\s*>", "\n", text)
        text = re.sub(r"(?i)</li\s*>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)

    # Normalize whitespace for cutting
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\u00a0", " ", text)

    # Cut quoted thread / history (Yandex, Outlook, Gmail, etc.)
    cut_patterns = (
        # Yandex / Mail.ru style separator before "Кому:"
        r"(?m)^\s*-{4,}\s*$",
        r"(?im)^\s*Кому:\s",
        r"(?im)^\s*Тема:\s",
        r"(?im)^\s*От:\s",
        r"(?im)^\s*Копия:\s",
        r"(?im)^\s*From:\s",
        r"(?im)^\s*To:\s",
        r"(?im)^\s*Subject:\s",
        r"(?im)^\s*Sent:\s",
        r"(?im)^\s*Date:\s",
        # "13.08.2026, 10:52, \"Name\" :"
        r"(?im)^\s*\d{1,2}[./]\d{1,2}[./]\d{2,4},\s*\d{1,2}:\d{2}\s*,\s*[\"«].+[\"»]\s*:\s*$",
        r"(?im)^\s*\d{1,2}[./]\d{1,2}[./]\d{2,4},\s*\d{1,2}:\d{2}\s*,\s*.+:\s*$",
        r"(?m)^>+",
        r"(?im)^-{2,}\s*Original Message",
        r"(?im)^-{2,}\s*Исходное сообщение",
        r"(?im)^-{2,}\s*Пересылаемое сообщение",
        r"(?is)<blockquote\b",
        r"(?im)^В\s+\d{1,2}[./]\d{1,2}[./]\d{2,4}.+писал[аи]?:",
        r"(?im)^On .+ wrote:",
        r"(?im)^Am .+ schrieb:",
        # Our own RFQ letters quoted back
        r"(?im)^Добрый день.*,\s*\nМеня зовут ",
        r"(?im)^Запрос коммерческого предложения",
        r"(?im)^Прошу направить коммерческое",
        r"(?im)^Уточнение по запросу",
        r"(?im)^\[RFQ-",
        r"(?im)^\[CHAT-",
        # Signature delimiter often starts quoted junk
        r"(?m)^--\s*$",
    )
    cut_at = None
    for marker in cut_patterns:
        m = re.search(marker, text)
        if m and m.start() > 15:
            if cut_at is None or m.start() < cut_at:
                cut_at = m.start()
    if cut_at is not None:
        text = text[:cut_at]

    # Drop trailing signature of the current reply (after real content)
    sig_cut = None
    for marker in (
        r"(?im)\n\s*С\s+[Уу]важением\s*[,!]?\s*\n",
        r"(?im)\n\s*Best\s+regards\s*[,!]?\s*\n",
        r"(?im)\n\s*With\s+best\s+regards\s*[,!]?\s*\n",
        r"(?im)\n\s*--\s*\n\s*У\s+[Уу]важением",
        r"(?im)\n\s*Менеджер по продажам\b",
        r"(?im)\n\s*Телефон:\s*\+",
        r"(?im)\n\s*Моб\.?\s*телефон:",
        r"(?im)\n\s*Электронная почта:",
        r"(?im)\n\s*Сайт:\s*https?://",
    ):
        m = re.search(marker, text)
        # only trim if signature is not the whole message
        if m and m.start() > 40:
            if sig_cut is None or m.start() < sig_cut:
                sig_cut = m.start()
    if sig_cut is not None:
        text = text[:sig_cut]

    text = re.sub(r"\n*-{3,}\s*$", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _qty_fit(requested: float | None, offered: float | None) -> str:
    if offered is None or requested is None:
        return "unknown"
    try:
        req = float(requested)
        off = float(offered)
    except (TypeError, ValueError):
        return "unknown"
    if req <= 0:
        return "unknown"
    ratio = off / req
    if abs(off - req) / req <= 0.02:
        return "full"
    if off < req:
        return "partial" if off > 0 else "none"
    return "exceeds"


def _heuristic_lines(body: str, products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    clean = strip_mail_noise(body)
    lower_body = clean.lower()
    for p in products:
        name = (p.get("name") or "").strip()
        if not name:
            continue
        idx = lower_body.find(name.lower())
        # if product only mentioned in leftover quote, still parse whole supplier reply
        window = clean[max(0, idx) : idx + 320] if idx >= 0 else clean
        if idx < 0:
            window = clean
        price = None
        currency = "RUB"
        m = PRICE_RE.search(window) or PRICE_RE.search(clean)
        if m:
            raw = m.group("price").replace(" ", "").replace(",", ".")
            try:
                price = float(raw)
            except ValueError:
                price = None
            chunk = m.group(0).lower()
            if "usd" in chunk or "$" in chunk:
                currency = "USD"
            elif "eur" in chunk or "€" in chunk:
                currency = "EUR"
        lead = None
        lm = LEAD_RE.search(window) or LEAD_RE.search(clean)
        if lm:
            lead = int(lm.group(1))
        in_stock = None
        if re.search(r"в наличии|на склад", clean, re.I):
            in_stock = 1
        elif re.search(r"под заказ|нет в наличии", clean, re.I):
            in_stock = 0
        offered = None
        qm = QTY_OFFER_RE.search(window) or QTY_OFFER_RE.search(clean)
        if qm:
            try:
                offered = float(qm.group(1).replace(",", "."))
            except ValueError:
                offered = None
        elif len(products) == 1:
            # single-item RFQ: plain "100 кг" in short reply is likely offer
            for pm in QTY_PLAIN_RE.finditer(clean):
                try:
                    val = float(pm.group(1).replace(",", "."))
                except ValueError:
                    continue
                req = float(p.get("quantity") or 0)
                # skip if equals requested qty and looks like echoed request
                if req and abs(val - req) < 1e-6 and "только" not in clean.lower():
                    continue
                offered = val
                break
        fit = _qty_fit(p.get("quantity"), offered)
        notes_bits = []
        if fit == "partial":
            notes_bits.append(
                f"Частичный объём: {offered} из {p.get('quantity')} {p.get('unit') or ''}".strip()
            )
        if price is not None or lead is not None or in_stock is not None or offered is not None or idx >= 0:
            lines.append(
                {
                    "rfq_item_id": p.get("rfq_item_id"),
                    "product_name": name,
                    "unit_price": price,
                    "currency": currency,
                    "vat_included": None,
                    "lead_time_days": lead,
                    "in_stock": in_stock,
                    "offered_qty": offered,
                    "qty_fit": fit,
                    "delivery_note": "",
                    "notes": "; ".join(notes_bits),
                }
            )
    return lines


def _heuristic_comparison(
    lines: list[dict[str, Any]], products: list[dict[str, Any]], body: str
) -> dict[str, Any]:
    mismatches: list[dict[str, str]] = []
    highlights: list[str] = []
    for line in lines:
        req = next((p for p in products if p.get("rfq_item_id") == line.get("rfq_item_id")), None)
        if not req:
            continue
        fit = line.get("qty_fit") or "unknown"
        offered = line.get("offered_qty")
        if fit == "partial":
            mismatches.append(
                {
                    "aspect": "объём",
                    "requested": f"{req.get('quantity')} {req.get('unit') or ''}".strip(),
                    "offered": f"{offered} {req.get('unit') or ''}".strip(),
                    "status": "partial",
                }
            )
            highlights.append(
                f"{line.get('product_name')}: в наличии {offered} из {req.get('quantity')} "
                f"{req.get('unit') or ''}".strip()
            )
        elif fit == "full":
            highlights.append(f"{line.get('product_name')}: объём полный")
        if line.get("unit_price") is not None:
            highlights.append(
                f"{line.get('product_name')}: {line['unit_price']} {line.get('currency') or 'RUB'}/ед."
            )
    clean = strip_mail_noise(body)
    delivery = ""
    other = ""
    for sent in re.split(r"[.!?\n]", clean):
        s = sent.strip()
        if not s:
            continue
        if re.search(r"доставк", s, re.I) and not delivery:
            delivery = s
        if re.search(r"меркури|ндс|оплат|производител", s, re.I):
            other = ((other + "; ") if other else "") + s
    if delivery and "доставк" in delivery.lower() and delivery not in mismatches:
        pass
    if other:
        mismatches.append(
            {
                "aspect": "прочие условия",
                "requested": "(см. запрос)",
                "offered": other[:300],
                "status": "unknown",
            }
        )
    verdict = "нужна проверка"
    if any(m.get("status") == "partial" for m in mismatches if m.get("aspect") == "объём"):
        verdict = "частичное покрытие запроса"
    elif lines and all((l.get("qty_fit") or "unknown") == "full" for l in lines):
        verdict = "объём совпадает"
    elif lines and any(l.get("unit_price") is not None for l in lines):
        verdict = "есть цена, объём уточнить"
    return {
        "verdict": verdict,
        "quantity_ok": not any(
            m.get("status") == "partial" for m in mismatches if m.get("aspect") == "объём"
        )
        and any((l.get("qty_fit") or "") == "full" for l in lines),
        "highlights": highlights,
        "mismatches": mismatches,
        "delivery": delivery,
        "payment": "",
        "other_conditions": other,
        "summary_text": "; ".join(highlights) if highlights else verdict,
    }


def _ai_parse(
    body: str,
    products: list[dict[str, Any]],
    *,
    rfq_notes: str = "",
    outgoing_body: str = "",
) -> dict[str, Any] | None:
    settings = get_settings()
    if not settings.ai_ready:
        return None

    from app.ai_client import AiError, chat_json

    catalog = [
        {
            "rfq_item_id": p["rfq_item_id"],
            "name": p["name"],
            "unit": p.get("unit"),
            "quantity": p.get("quantity"),
            "item_note": p.get("note") or "",
        }
        for p in products
    ]
    clean = strip_mail_noise(body)
    system = (
        "Ты аналитик закупок. Сравни ответ поставщика с нашим запросом КП. "
        "Мы — покупатель; поставщик отвечает на наш запрос. "
        "Верни строго JSON-объект:\n"
        "{\n"
        '  "lines": [{\n'
        '    "rfq_item_id": number|null,\n'
        '    "product_name": string,\n'
        '    "unit_price": number|null,\n'
        '    "currency": "RUB"|"USD"|"EUR",\n'
        '    "vat_included": true|false|null,\n'
        '    "lead_time_days": number|null,\n'
        '    "in_stock": true|false|null,\n'
        '    "offered_qty": number|null,\n'
        '    "qty_fit": "full"|"partial"|"none"|"exceeds"|"unknown",\n'
        '    "delivery_note": string,\n'
        '    "notes": string\n'
        "  }],\n"
        '  "comparison": {\n'
        '    "verdict": string,\n'
        '    "quantity_ok": boolean,\n'
        '    "highlights": [string],\n'
        '    "mismatches": [{"aspect":string,"requested":string,"offered":string,'
        '"status":"match"|"partial"|"mismatch"|"unknown"}],\n'
        '    "delivery": string,\n'
        '    "payment": string,\n'
        '    "other_conditions": string,\n'
        '    "summary_text": string\n'
        "  }\n"
        "}\n"
        "Правила:\n"
        "- Цена за единицу (кг/шт), не общая сумма, если явно не сказано иначе.\n"
        "- Если запрошено 250 кг, а поставщик пишет «только 100 кг» — offered_qty=100, qty_fit=partial.\n"
        "- Сравни условия из запроса (производитель, Меркурий, срок, доставка, оплата) с ответом.\n"
        "- Не выдумывай факты: только то, что есть в письме поставщика.\n"
        "- Смотри в первую очередь текст ответа поставщика, цитату нашего письма игнорируй как источник КП.\n"
        "Без markdown."
    )
    user = (
        f"Позиции запроса:\n{json.dumps(catalog, ensure_ascii=False)}\n\n"
        f"Общие условия/заметки запроса:\n{(rfq_notes or '—')[:2000]}\n\n"
        f"Текст нашего исходящего письма (для контекста условий):\n{(outgoing_body or '—')[:4000]}\n\n"
        f"Ответ поставщика:\n{clean[:10000]}"
    )

    try:
        data = chat_json(system=system, user=user, temperature=0.1, timeout=75.0)
    except AiError:
        log.exception("AI parse failed")
        return None
    except Exception:
        log.exception("AI parse failed")
        return None

    # backward compatible: bare list of lines
    if isinstance(data, list):
        return {"lines": data, "comparison": None}
    if not isinstance(data, dict):
        return None
    return data


def _normalize_line(row: dict[str, Any], products: list[dict[str, Any]]) -> dict[str, Any]:
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
    offered = row.get("offered_qty")
    try:
        offered = float(offered) if offered is not None else None
    except (TypeError, ValueError):
        offered = None
    vat = row.get("vat_included")
    stock = row.get("in_stock")
    rid = row.get("rfq_item_id")
    try:
        rid = int(rid) if rid is not None else None
    except (TypeError, ValueError):
        rid = None
    req = next((p for p in products if p.get("rfq_item_id") == rid), None)
    fit = str(row.get("qty_fit") or "").lower().strip()
    if fit not in {"full", "partial", "none", "exceeds", "unknown"}:
        fit = _qty_fit(req.get("quantity") if req else None, offered)
    return {
        "rfq_item_id": rid,
        "product_name": str(row.get("product_name") or (req or {}).get("name") or ""),
        "unit_price": price,
        "currency": str(row.get("currency") or "RUB"),
        "vat_included": 1 if vat is True else (0 if vat is False else None),
        "lead_time_days": lead,
        "in_stock": 1 if stock is True else (0 if stock is False else None),
        "offered_qty": offered,
        "qty_fit": fit,
        "delivery_note": str(row.get("delivery_note") or "")[:500],
        "notes": str(row.get("notes") or "")[:1000],
    }


def parse_and_store_reply(conn, reply_id: int) -> dict[str, Any]:
    reply = conn.execute("SELECT * FROM quote_replies WHERE id=?", (reply_id,)).fetchone()
    if not reply:
        raise ValueError("reply not found")

    msg = conn.execute(
        "SELECT * FROM rfq_messages WHERE id=?",
        (reply["rfq_message_id"],),
    ).fetchone()
    rfq = conn.execute("SELECT * FROM rfqs WHERE id=?", (msg["rfq_id"],)).fetchone()
    products = conn.execute(
        """
        SELECT ri.id AS rfq_item_id, p.name, ri.unit, ri.quantity, ri.note
        FROM rfq_items ri
        JOIN products p ON p.id = ri.product_id
        WHERE ri.rfq_id = ?
        """,
        (msg["rfq_id"],),
    ).fetchall()
    product_dicts = [dict(p) for p in products]

    source = "heuristic"
    comparison: dict[str, Any] | None = None
    lines: list[dict[str, Any]] = []

    ai_data = _ai_parse(
        reply["raw_body"],
        product_dicts,
        rfq_notes=(rfq["notes"] if rfq else "") or "",
        outgoing_body=(msg["body"] if msg else "") or "",
    )
    if ai_data:
        source = "ai"
        raw_lines = ai_data.get("lines") if isinstance(ai_data, dict) else None
        if isinstance(raw_lines, list):
            lines = [_normalize_line(r, product_dicts) for r in raw_lines if isinstance(r, dict)]
        comparison = ai_data.get("comparison") if isinstance(ai_data.get("comparison"), dict) else None

    if not lines:
        lines = _heuristic_lines(reply["raw_body"], product_dicts)
        source = "heuristic" if source != "ai" else "ai+heuristic"
        comparison = comparison or _heuristic_comparison(lines, product_dicts, reply["raw_body"])
    elif not comparison:
        comparison = _heuristic_comparison(lines, product_dicts, reply["raw_body"])

    # ensure qty_fit filled
    for line in lines:
        if not line.get("qty_fit") or line["qty_fit"] == "unknown":
            req = next(
                (p for p in product_dicts if p.get("rfq_item_id") == line.get("rfq_item_id")),
                None,
            )
            line["qty_fit"] = _qty_fit(req.get("quantity") if req else None, line.get("offered_qty"))

    conn.execute("DELETE FROM quote_lines WHERE quote_reply_id=?", (reply_id,))
    for line in lines:
        conn.execute(
            """
            INSERT INTO quote_lines
              (quote_reply_id, rfq_item_id, product_name, unit_price, currency,
               vat_included, lead_time_days, in_stock, notes,
               offered_qty, qty_fit, delivery_note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                line.get("offered_qty"),
                line.get("qty_fit") or "unknown",
                line.get("delivery_note") or "",
            ),
        )

    summary_text = ""
    if comparison:
        summary_text = str(comparison.get("summary_text") or comparison.get("verdict") or "")
    conn.execute(
        """
        UPDATE quote_replies
        SET parse_status=?, parse_error=NULL, comparison_summary=?, comparison_json=?
        WHERE id=?
        """,
        (
            f"ok:{source}",
            summary_text[:2000],
            json.dumps(comparison or {}, ensure_ascii=False),
            reply_id,
        ),
    )
    return {"lines": len(lines), "source": source, "comparison": comparison}


def parse_and_store_chat_message(conn, message_id: int) -> dict[str, Any]:
    """Parse inbound supplier_chat_messages into chat_quote_lines + comparison."""
    msg = conn.execute(
        "SELECT * FROM supplier_chat_messages WHERE id=? AND direction='in'",
        (message_id,),
    ).fetchone()
    if not msg:
        raise ValueError("Сообщение не найдено")

    chat = conn.execute("SELECT * FROM supplier_chats WHERE id=?", (msg["chat_id"],)).fetchone()
    items = conn.execute(
        """
        SELECT i.id AS rfq_item_id,
               COALESCE(NULLIF(i.custom_name,''), p.name, 'Позиция') AS name,
               i.unit, i.quantity, i.note
        FROM supplier_chat_items i
        LEFT JOIN products p ON p.id=i.product_id
        WHERE i.chat_id=?
        """,
        (msg["chat_id"],),
    ).fetchall()
    product_dicts = [dict(p) for p in items]

    out_row = conn.execute(
        """
        SELECT body FROM supplier_chat_messages
        WHERE chat_id=? AND direction='out' AND status='sent'
        ORDER BY sent_at DESC, id DESC LIMIT 1
        """,
        (msg["chat_id"],),
    ).fetchone()
    outgoing = (out_row["body"] if out_row else "") or ""
    conditions = ""
    if chat and "letter_conditions" in chat.keys():
        conditions = chat["letter_conditions"] or ""

    source = "heuristic"
    comparison: dict[str, Any] | None = None
    lines: list[dict[str, Any]] = []

    ai_data = _ai_parse(
        msg["body"] or "",
        product_dicts,
        rfq_notes=conditions,
        outgoing_body=outgoing,
    )
    if ai_data:
        source = "ai"
        raw_lines = ai_data.get("lines") if isinstance(ai_data, dict) else None
        if isinstance(raw_lines, list):
            lines = [_normalize_line(r, product_dicts) for r in raw_lines if isinstance(r, dict)]
        comparison = (
            ai_data.get("comparison") if isinstance(ai_data.get("comparison"), dict) else None
        )

    if not lines:
        lines = _heuristic_lines(msg["body"] or "", product_dicts)
        source = "heuristic" if source != "ai" else "ai+heuristic"
        comparison = comparison or _heuristic_comparison(lines, product_dicts, msg["body"] or "")
    elif not comparison:
        comparison = _heuristic_comparison(lines, product_dicts, msg["body"] or "")

    for line in lines:
        if not line.get("qty_fit") or line["qty_fit"] == "unknown":
            req = next(
                (p for p in product_dicts if p.get("rfq_item_id") == line.get("rfq_item_id")),
                None,
            )
            line["qty_fit"] = _qty_fit(req.get("quantity") if req else None, line.get("offered_qty"))

    conn.execute("DELETE FROM chat_quote_lines WHERE chat_message_id=?", (message_id,))
    for line in lines:
        conn.execute(
            """
            INSERT INTO chat_quote_lines
              (chat_message_id, chat_item_id, product_name, unit_price, currency,
               vat_included, lead_time_days, in_stock, notes,
               offered_qty, qty_fit, delivery_note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                line.get("rfq_item_id"),
                line.get("product_name") or "",
                line.get("unit_price"),
                line.get("currency") or "RUB",
                line.get("vat_included"),
                line.get("lead_time_days"),
                line.get("in_stock"),
                line.get("notes") or "",
                line.get("offered_qty"),
                line.get("qty_fit") or "unknown",
                line.get("delivery_note") or "",
            ),
        )

    summary_text = ""
    if comparison:
        summary_text = str(comparison.get("summary_text") or comparison.get("verdict") or "")
    conn.execute(
        """
        UPDATE supplier_chat_messages
        SET parse_status=?, comparison_summary=?, comparison_json=?
        WHERE id=?
        """,
        (
            f"ok:{source}",
            summary_text[:2000],
            json.dumps(comparison or {}, ensure_ascii=False),
            message_id,
        ),
    )
    return {"lines": len(lines), "source": source, "comparison": comparison}
