"""Disk storage for message attachments (KP, invoices, outbound files)."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.db import rows_to_dicts

MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_FILES_PER_MESSAGE = 5
ALLOWED_EXT = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".txt",
    ".csv",
    ".zip",
    ".rar",
    ".7z",
}


def attachments_root() -> Path:
    root = get_settings().db_path.parent / "attachments"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_filename(name: str) -> str:
    base = Path(name or "file").name
    base = re.sub(r"[^\w.\- ()а-яА-ЯёЁ]+", "_", base, flags=re.U).strip("._ ") or "file"
    return base[:180]


def guess_kind(filename: str, mime: str = "") -> str:
    name = (filename or "").lower()
    mime = (mime or "").lower()
    if any(x in name for x in ("счет", "счёт", "invoice", "schet")):
        return "invoice"
    if any(x in name for x in ("кп", "kp", "offer", "коммерч", "quote", "pric")):
        return "kp"
    if mime.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
        return "image"
    if name.endswith(".pdf") or mime == "application/pdf":
        return "pdf"
    return "other"


def save_upload(
    conn,
    *,
    owner_type: str,
    owner_id: int,
    filename: str,
    data: bytes,
    mime: str = "",
    kind: str = "",
) -> dict[str, Any]:
    if not data:
        raise ValueError("Пустой файл")
    if len(data) > MAX_FILE_BYTES:
        raise ValueError(f"Файл больше {MAX_FILE_BYTES // (1024 * 1024)} МБ")
    safe = _safe_filename(filename)
    ext = Path(safe).suffix.lower()
    if ext and ext not in ALLOWED_EXT:
        raise ValueError(f"Тип файла не поддерживается: {ext}")
    existing = conn.execute(
        "SELECT COUNT(*) AS c FROM message_files WHERE owner_type=? AND owner_id=?",
        (owner_type, owner_id),
    ).fetchone()["c"]
    if int(existing) >= MAX_FILES_PER_MESSAGE:
        raise ValueError(f"Не больше {MAX_FILES_PER_MESSAGE} файлов на сообщение")

    stored = f"{uuid.uuid4().hex}_{safe}"
    path = attachments_root() / stored
    path.write_bytes(data)
    mime = (mime or "application/octet-stream").strip() or "application/octet-stream"
    kind = (kind or guess_kind(safe, mime)).strip() or "other"
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """
        INSERT INTO message_files
          (owner_type, owner_id, filename, stored_name, mime, size_bytes, kind, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (owner_type, owner_id, safe, stored, mime, len(data), kind, now),
    )
    return {
        "id": int(cur.lastrowid),
        "filename": safe,
        "stored_name": stored,
        "mime": mime,
        "size_bytes": len(data),
        "kind": kind,
        "path": str(path),
    }


def list_files(conn, owner_type: str, owner_id: int) -> list[dict]:
    return rows_to_dicts(
        conn.execute(
            """
            SELECT * FROM message_files
            WHERE owner_type=? AND owner_id=?
            ORDER BY id
            """,
            (owner_type, owner_id),
        ).fetchall()
    )


def list_files_for_owners(conn, owner_type: str, owner_ids: list[int]) -> dict[int, list[dict]]:
    if not owner_ids:
        return {}
    ph = ",".join("?" * len(owner_ids))
    rows = rows_to_dicts(
        conn.execute(
            f"""
            SELECT * FROM message_files
            WHERE owner_type=? AND owner_id IN ({ph})
            ORDER BY id
            """,
            [owner_type, *owner_ids],
        ).fetchall()
    )
    out: dict[int, list[dict]] = {int(i): [] for i in owner_ids}
    for r in rows:
        out.setdefault(int(r["owner_id"]), []).append(r)
    return out


def get_file(conn, file_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM message_files WHERE id=?", (file_id,)).fetchone()
    return dict(row) if row else None


def resolve_path(stored_name: str) -> Path:
    name = Path(stored_name or "").name
    return attachments_root() / name


def file_payloads_for_send(conn, owner_type: str, owner_id: int) -> list[tuple[str, bytes, str]]:
    """Return (filename, bytes, mime) for SMTP attachments."""
    out: list[tuple[str, bytes, str]] = []
    for f in list_files(conn, owner_type, owner_id):
        path = resolve_path(f["stored_name"])
        if not path.is_file():
            continue
        out.append((f["filename"], path.read_bytes(), f.get("mime") or "application/octet-stream"))
    return out


def kind_label(kind: str) -> str:
    return {
        "kp": "КП",
        "invoice": "Счёт",
        "pdf": "PDF",
        "image": "Фото",
        "other": "Файл",
    }.get((kind or "").lower(), "Файл")
