from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from app.config import get_settings

DEFAULT_TEMPLATE_SUBJECT = "Запрос коммерческого предложения {{ token }}"
DEFAULT_TEMPLATE_BODY = """Добрый день{% if contact %}, {{ contact }}{% endif %}!

Меня зовут {{ sender_name }}{% if company %}, {{ company }}{% endif %}.
Прошу направить коммерческое предложение по позициям ниже.

{% for item in items %}
{{ loop.index }}. {{ item.name }} — {{ item.quantity }} {{ item.unit }}{% if item.note %} ({{ item.note }}){% endif %}
{% endfor %}

Буду благодарен за информацию о:
— цене за единицу (с НДС / без НДС);
— сроке поставки;
— наличии на складе.

Если по какой-то позиции поставка невозможна — напишите, пожалуйста, об этом отдельно.

С уважением,
{{ sender_name }}{% if company %}
{{ company }}{% endif %}
{% if reply_hint %}
---
Ответьте, пожалуйста, на это письмо (в теме оставьте код {{ token }}).
{% endif %}
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_db() -> sqlite3.Connection:
    path = get_settings().db_path
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_session() -> Iterator[sqlite3.Connection]:
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def rows_to_dicts(rows: list[sqlite3.Row] | None) -> list[dict[str, Any]]:
    if not rows:
        return []
    return [dict(r) for r in rows]


def init_db() -> None:
    with db_session() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS categories (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS products (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
              name TEXT NOT NULL,
              unit TEXT NOT NULL DEFAULT 'шт',
              notes TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              UNIQUE(category_id, name)
            );

            CREATE TABLE IF NOT EXISTS suppliers (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              email TEXT NOT NULL,
              contact_person TEXT NOT NULL DEFAULT '',
              phone TEXT NOT NULL DEFAULT '',
              notes TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS product_suppliers (
              product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
              supplier_id INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
              PRIMARY KEY (product_id, supplier_id)
            );

            CREATE TABLE IF NOT EXISTS email_templates (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              subject_tpl TEXT NOT NULL,
              body_tpl TEXT NOT NULL,
              is_default INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS rfqs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'draft',
              notes TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              sent_at TEXT
            );

            CREATE TABLE IF NOT EXISTS rfq_items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              rfq_id INTEGER NOT NULL REFERENCES rfqs(id) ON DELETE CASCADE,
              product_id INTEGER NOT NULL REFERENCES products(id),
              quantity REAL NOT NULL DEFAULT 1,
              unit TEXT NOT NULL DEFAULT 'шт',
              note TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS rfq_messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              rfq_id INTEGER NOT NULL REFERENCES rfqs(id) ON DELETE CASCADE,
              supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
              token TEXT NOT NULL UNIQUE,
              subject TEXT NOT NULL,
              body TEXT NOT NULL,
              message_id TEXT,
              status TEXT NOT NULL DEFAULT 'pending',
              error TEXT,
              sent_at TEXT
            );

            CREATE TABLE IF NOT EXISTS quote_replies (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              rfq_message_id INTEGER NOT NULL REFERENCES rfq_messages(id) ON DELETE CASCADE,
              message_id TEXT NOT NULL,
              from_addr TEXT NOT NULL DEFAULT '',
              subject TEXT NOT NULL DEFAULT '',
              raw_body TEXT NOT NULL DEFAULT '',
              parse_status TEXT NOT NULL DEFAULT 'pending',
              parse_error TEXT,
              received_at TEXT NOT NULL,
              UNIQUE(message_id)
            );

            CREATE TABLE IF NOT EXISTS quote_lines (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              quote_reply_id INTEGER NOT NULL REFERENCES quote_replies(id) ON DELETE CASCADE,
              rfq_item_id INTEGER REFERENCES rfq_items(id) ON DELETE SET NULL,
              product_name TEXT NOT NULL DEFAULT '',
              unit_price REAL,
              currency TEXT NOT NULL DEFAULT 'RUB',
              vat_included INTEGER,
              lead_time_days INTEGER,
              in_stock INTEGER,
              notes TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_products_category ON products(category_id);
            CREATE INDEX IF NOT EXISTS idx_rfq_items_rfq ON rfq_items(rfq_id);
            CREATE INDEX IF NOT EXISTS idx_rfq_messages_rfq ON rfq_messages(rfq_id);
            CREATE INDEX IF NOT EXISTS idx_rfq_messages_token ON rfq_messages(token);
            CREATE INDEX IF NOT EXISTS idx_quote_replies_msg ON quote_replies(rfq_message_id);
            """
        )
        row = conn.execute("SELECT id FROM email_templates WHERE is_default=1 LIMIT 1").fetchone()
        if not row:
            conn.execute(
                """
                INSERT INTO email_templates (name, subject_tpl, body_tpl, is_default)
                VALUES (?, ?, ?, 1)
                """,
                ("Стандартный запрос КП", DEFAULT_TEMPLATE_SUBJECT, DEFAULT_TEMPLATE_BODY),
            )


def ensure_seed_demo(conn: sqlite3.Connection) -> None:
    """Optional tiny seed if DB empty — helps first launch."""
    n = conn.execute("SELECT COUNT(*) AS c FROM categories").fetchone()["c"]
    if n:
        return
    now = _now()
    cur = conn.execute("INSERT INTO categories (name, created_at) VALUES (?, ?)", ("Общее", now))
    cat_id = cur.lastrowid
    conn.execute(
        "INSERT INTO products (category_id, name, unit, notes, created_at) VALUES (?, ?, ?, ?, ?)",
        (cat_id, "Пример товара", "шт", "Удалите или отредактируйте", now),
    )
