from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from app.config import get_settings

DEFAULT_TEMPLATE_SUBJECT = "Запрос коммерческого предложения {{ token }}"
DEFAULT_TEMPLATE_BODY = """Добрый день{% if contact %}, {{ contact }}{% endif %}!

Меня зовут {{ sender_name }}{% if position %}, {{ position }}{% endif %}{% if company %} ({{ company }}){% endif %}.
Прошу направить коммерческое предложение на закупку следующих позиций:

{% for item in items %}
{{ loop.index }}. {{ item.name }} — {{ item.quantity }} {{ item.unit }}{% if item.note %} ({{ item.note }}){% endif %}
{% endfor %}

{% if conditions %}
Дополнительные условия и требования по этому запросу:
{{ conditions }}

{% endif %}Просьба указать по каждой позиции:
— цену за единицу (с НДС / без НДС);
— срок поставки;
— наличие на складе / срок изготовления.

Мы рассматриваем вас как поставщика и будем признательны за КП.
Если позиция недоступна — напишите об этом, пожалуйста.

С уважением,
{{ sender_name }}{% if position %}
{{ position }}{% endif %}{% if company %}
{{ company }}{% endif %}{% if phone %}
тел. {{ phone }}{% endif %}{% if website %}
{{ website }}{% endif %}{% if signature_note %}
{{ signature_note }}{% endif %}
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
              website TEXT NOT NULL DEFAULT '',
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
              letter_conditions TEXT NOT NULL DEFAULT '',
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
              comparison_summary TEXT NOT NULL DEFAULT '',
              comparison_json TEXT NOT NULL DEFAULT '',
              source_channel TEXT NOT NULL DEFAULT 'email',
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
              notes TEXT NOT NULL DEFAULT '',
              offered_qty REAL,
              qty_fit TEXT NOT NULL DEFAULT 'unknown',
              delivery_note TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS search_sessions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              query TEXT NOT NULL,
              product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
              status TEXT NOT NULL DEFAULT 'running',
              summary TEXT NOT NULL DEFAULT '',
              raw_hits TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS search_findings (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              session_id INTEGER NOT NULL REFERENCES search_sessions(id) ON DELETE CASCADE,
              company_name TEXT NOT NULL,
              website TEXT NOT NULL DEFAULT '',
              email TEXT NOT NULL DEFAULT '',
              phone TEXT NOT NULL DEFAULT '',
              contact_person TEXT NOT NULL DEFAULT '',
              price_info TEXT NOT NULL DEFAULT '',
              product_match TEXT NOT NULL DEFAULT '',
              source_url TEXT NOT NULL DEFAULT '',
              notes TEXT NOT NULL DEFAULT '',
              confidence TEXT NOT NULL DEFAULT 'medium',
              added_supplier_id INTEGER REFERENCES suppliers(id) ON DELETE SET NULL,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS manager_profile (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              sender_name TEXT NOT NULL DEFAULT '',
              sender_company TEXT NOT NULL DEFAULT '',
              sender_position TEXT NOT NULL DEFAULT '',
              sender_phone TEXT NOT NULL DEFAULT '',
              updated_at TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS rfq_followups (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              rfq_id INTEGER NOT NULL REFERENCES rfqs(id) ON DELETE CASCADE,
              rfq_message_id INTEGER NOT NULL REFERENCES rfq_messages(id) ON DELETE CASCADE,
              supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
              subject TEXT NOT NULL DEFAULT '',
              body TEXT NOT NULL,
              message_id TEXT,
              status TEXT NOT NULL DEFAULT 'pending',
              error TEXT,
              created_at TEXT NOT NULL,
              sent_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_products_category ON products(category_id);
            CREATE INDEX IF NOT EXISTS idx_rfq_items_rfq ON rfq_items(rfq_id);
            CREATE INDEX IF NOT EXISTS idx_rfq_messages_rfq ON rfq_messages(rfq_id);
            CREATE INDEX IF NOT EXISTS idx_rfq_messages_token ON rfq_messages(token);
            CREATE INDEX IF NOT EXISTS idx_quote_replies_msg ON quote_replies(rfq_message_id);
            CREATE INDEX IF NOT EXISTS idx_search_sessions_at ON search_sessions(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_followups_msg ON rfq_followups(rfq_message_id);

            CREATE TABLE IF NOT EXISTS supplier_chats (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
              title TEXT NOT NULL DEFAULT '',
              token TEXT NOT NULL UNIQUE,
              reply_email TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'open',
              notes TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS supplier_chat_items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id INTEGER NOT NULL REFERENCES supplier_chats(id) ON DELETE CASCADE,
              product_id INTEGER REFERENCES products(id),
              custom_name TEXT NOT NULL DEFAULT '',
              quantity REAL NOT NULL DEFAULT 1,
              unit TEXT NOT NULL DEFAULT 'шт',
              note TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS supplier_chat_messages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_id INTEGER NOT NULL REFERENCES supplier_chats(id) ON DELETE CASCADE,
              direction TEXT NOT NULL DEFAULT 'out',
              kind TEXT NOT NULL DEFAULT 'email',
              subject TEXT NOT NULL DEFAULT '',
              body TEXT NOT NULL DEFAULT '',
              message_id TEXT,
              to_addr TEXT NOT NULL DEFAULT '',
              from_addr TEXT NOT NULL DEFAULT '',
              source_channel TEXT NOT NULL DEFAULT 'email',
              status TEXT NOT NULL DEFAULT 'pending',
              error TEXT,
              created_at TEXT NOT NULL,
              sent_at TEXT
            );

            CREATE TABLE IF NOT EXISTS message_files (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              owner_type TEXT NOT NULL,
              owner_id INTEGER NOT NULL,
              filename TEXT NOT NULL,
              stored_name TEXT NOT NULL,
              mime TEXT NOT NULL DEFAULT 'application/octet-stream',
              size_bytes INTEGER NOT NULL DEFAULT 0,
              kind TEXT NOT NULL DEFAULT 'other',
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS company_accounts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              label TEXT NOT NULL DEFAULT '',
              legal_name TEXT NOT NULL DEFAULT '',
              inn TEXT NOT NULL DEFAULT '',
              kpp TEXT NOT NULL DEFAULT '',
              bank_name TEXT NOT NULL DEFAULT '',
              bik TEXT NOT NULL DEFAULT '',
              checking_account TEXT NOT NULL DEFAULT '',
              corr_account TEXT NOT NULL DEFAULT '',
              address TEXT NOT NULL DEFAULT '',
              is_default INTEGER NOT NULL DEFAULT 0,
              sort_order INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS chat_quote_lines (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              chat_message_id INTEGER NOT NULL REFERENCES supplier_chat_messages(id) ON DELETE CASCADE,
              chat_item_id INTEGER REFERENCES supplier_chat_items(id) ON DELETE SET NULL,
              product_name TEXT NOT NULL DEFAULT '',
              unit_price REAL,
              currency TEXT NOT NULL DEFAULT 'RUB',
              vat_included INTEGER,
              lead_time_days INTEGER,
              in_stock INTEGER,
              notes TEXT NOT NULL DEFAULT '',
              offered_qty REAL,
              qty_fit TEXT NOT NULL DEFAULT 'unknown',
              delivery_note TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_chats_supplier ON supplier_chats(supplier_id);
            CREATE INDEX IF NOT EXISTS idx_chats_token ON supplier_chats(token);
            CREATE INDEX IF NOT EXISTS idx_chat_msgs ON supplier_chat_messages(chat_id);
            CREATE INDEX IF NOT EXISTS idx_msg_files_owner ON message_files(owner_type, owner_id);
            CREATE INDEX IF NOT EXISTS idx_chat_quote_lines ON chat_quote_lines(chat_message_id);
            """
        )
        _ensure_column(conn, "suppliers", "website", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "rfqs", "letter_conditions", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "quote_replies", "comparison_summary", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "quote_replies", "comparison_json", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "quote_replies", "source_channel", "TEXT NOT NULL DEFAULT 'email'")
        _ensure_column(conn, "quote_lines", "offered_qty", "REAL")
        _ensure_column(conn, "quote_lines", "qty_fit", "TEXT NOT NULL DEFAULT 'unknown'")
        _ensure_column(conn, "quote_lines", "delivery_note", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "rfq_messages", "reply_email", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "rfq_followups", "to_addr", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "supplier_chats", "letter_conditions", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "supplier_chat_items", "custom_name", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "supplier_chat_messages", "parse_status", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "supplier_chat_messages", "comparison_summary", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "supplier_chat_messages", "comparison_json", "TEXT NOT NULL DEFAULT ''")
        # Existing rows treated as read; new inserts set is_read=0 explicitly
        _ensure_column(conn, "quote_replies", "is_read", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "supplier_chat_messages", "is_read", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(conn, "manager_profile", "sender_website", "TEXT NOT NULL DEFAULT ''")
        _ensure_column(conn, "manager_profile", "signature_note", "TEXT NOT NULL DEFAULT ''")
        _migrate_chat_items_nullable_product(conn)
        row = conn.execute("SELECT id FROM email_templates WHERE is_default=1 LIMIT 1").fetchone()
        if not row:
            conn.execute(
                """
                INSERT INTO email_templates (name, subject_tpl, body_tpl, is_default)
                VALUES (?, ?, ?, 1)
                """,
                ("Стандартный запрос КП", DEFAULT_TEMPLATE_SUBJECT, DEFAULT_TEMPLATE_BODY),
            )
        else:
            # Keep subject/body in sync with buyer-RFQ wording unless user renamed template
            name = conn.execute(
                "SELECT name FROM email_templates WHERE id=?", (row["id"],)
            ).fetchone()
            if name and name["name"] in ("Стандартный запрос КП", "Стандартный"):
                conn.execute(
                    "UPDATE email_templates SET subject_tpl=?, body_tpl=? WHERE id=?",
                    (DEFAULT_TEMPLATE_SUBJECT, DEFAULT_TEMPLATE_BODY, row["id"]),
                )

        # Seed manager profile from .env once
        prof = conn.execute("SELECT id FROM manager_profile WHERE id=1").fetchone()
        if not prof:
            s = get_settings()
            conn.execute(
                """
                INSERT INTO manager_profile
                  (id, sender_name, sender_company, sender_position, sender_phone, updated_at)
                VALUES (1, ?, ?, '', '', ?)
                """,
                (s.sender_name or "Отдел закупок", s.sender_company or "", _now()),
            )


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _migrate_chat_items_nullable_product(conn: sqlite3.Connection) -> None:
    """Allow free-form lines without catalog product_id."""
    rows = list(conn.execute("PRAGMA table_info(supplier_chat_items)").fetchall())
    if not rows:
        return
    by_name = {r[1]: r for r in rows}
    prod = by_name.get("product_id")
    # r[3] is notnull flag
    if prod is None or int(prod[3] or 0) == 0:
        return
    conn.execute(
        """
        CREATE TABLE supplier_chat_items_new (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          chat_id INTEGER NOT NULL REFERENCES supplier_chats(id) ON DELETE CASCADE,
          product_id INTEGER REFERENCES products(id),
          custom_name TEXT NOT NULL DEFAULT '',
          quantity REAL NOT NULL DEFAULT 1,
          unit TEXT NOT NULL DEFAULT 'шт',
          note TEXT NOT NULL DEFAULT ''
        )
        """
    )
    has_custom = "custom_name" in by_name
    if has_custom:
        conn.execute(
            """
            INSERT INTO supplier_chat_items_new
              (id, chat_id, product_id, custom_name, quantity, unit, note)
            SELECT id, chat_id, product_id, COALESCE(custom_name,''), quantity, unit, note
            FROM supplier_chat_items
            """
        )
    else:
        conn.execute(
            """
            INSERT INTO supplier_chat_items_new
              (id, chat_id, product_id, custom_name, quantity, unit, note)
            SELECT id, chat_id, product_id, '', quantity, unit, note
            FROM supplier_chat_items
            """
        )
    conn.execute("DROP TABLE supplier_chat_items")
    conn.execute("ALTER TABLE supplier_chat_items_new RENAME TO supplier_chat_items")


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
