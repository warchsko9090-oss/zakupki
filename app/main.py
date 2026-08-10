from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db import ensure_seed_demo, get_db, init_db, rows_to_dicts
from app.scheduler import start_scheduler
from app import services

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Закупки")
settings = get_settings()
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie="zakupki")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    conn = get_db()
    try:
        ensure_seed_demo(conn)
        conn.commit()
    finally:
        conn.close()
    start_scheduler()


def require_auth(request: Request) -> bool:
    return bool(request.session.get("auth"))


def money(v) -> str:
    try:
        if v is None:
            return "—"
        num = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"{num:,.2f}".replace(",", " ").replace(".", ",")


templates.env.filters["money"] = money


def _flash(request: Request, message: str, kind: str = "ok") -> None:
    request.session["flash"] = {"message": message, "kind": kind}


def _pop_flash(request: Request) -> dict | None:
    return request.session.pop("flash", None)


def _ctx(request: Request, **extra: Any) -> dict[str, Any]:
    return {
        "request": request,
        "app_name": settings.app_name,
        "flash": _pop_flash(request),
        "mail_ready": settings.mail_ready,
        "ai_ready": settings.ai_ready,
        **extra,
    }


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if require_auth(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", _ctx(request))


@app.post("/login")
async def login_post(request: Request, password: str = Form(...)):
    if password == settings.app_password:
        request.session["auth"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        _ctx(request, error="Неверный пароль"),
        status_code=401,
    )


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        stats = {
            "categories": conn.execute("SELECT COUNT(*) c FROM categories").fetchone()["c"],
            "products": conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"],
            "suppliers": conn.execute("SELECT COUNT(*) c FROM suppliers").fetchone()["c"],
            "rfqs": conn.execute("SELECT COUNT(*) c FROM rfqs").fetchone()["c"],
            "replies": conn.execute("SELECT COUNT(*) c FROM quote_replies").fetchone()["c"],
        }
        recent = rows_to_dicts(
            conn.execute(
                "SELECT * FROM rfqs ORDER BY created_at DESC LIMIT 8"
            ).fetchall()
        )
    finally:
        conn.close()
    return templates.TemplateResponse("home.html", _ctx(request, stats=stats, recent=recent))


# ── Catalog ──────────────────────────────────────────────


@app.get("/catalog", response_class=HTMLResponse)
async def catalog(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        categories = services.list_categories(conn)
        products = services.list_products(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(
        "catalog.html",
        _ctx(request, categories=categories, products=products),
    )


@app.post("/catalog/categories")
async def add_category(request: Request, name: str = Form(...)):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO categories (name, created_at) VALUES (?, ?)",
            (name.strip(), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        _flash(request, f"Категория «{name.strip()}» добавлена")
    except Exception as exc:
        _flash(request, str(exc), "err")
    finally:
        conn.close()
    return RedirectResponse("/catalog", status_code=303)


@app.post("/catalog/categories/{category_id}/delete")
async def delete_category(request: Request, category_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        conn.execute("DELETE FROM categories WHERE id=?", (category_id,))
        conn.commit()
        _flash(request, "Категория удалена")
    finally:
        conn.close()
    return RedirectResponse("/catalog", status_code=303)


@app.post("/catalog/products")
async def add_product(
    request: Request,
    category_id: int = Form(...),
    name: str = Form(...),
    unit: str = Form("шт"),
    notes: str = Form(""),
):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO products (category_id, name, unit, notes, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                category_id,
                name.strip(),
                (unit or "шт").strip(),
                notes.strip(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        _flash(request, f"Товар «{name.strip()}» добавлен")
    except Exception as exc:
        _flash(request, f"Не удалось добавить товар: {exc}", "err")
    finally:
        conn.close()
    return RedirectResponse("/catalog", status_code=303)


@app.post("/catalog/products/{product_id}/delete")
async def delete_product(request: Request, product_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        conn.execute("DELETE FROM products WHERE id=?", (product_id,))
        conn.commit()
        _flash(request, "Товар удалён")
    finally:
        conn.close()
    return RedirectResponse("/catalog", status_code=303)


# ── Suppliers ────────────────────────────────────────────


@app.get("/suppliers", response_class=HTMLResponse)
async def suppliers_page(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        suppliers = services.list_suppliers(conn)
        products = services.list_products(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(
        "suppliers.html",
        _ctx(request, suppliers=suppliers, products=products),
    )


@app.post("/suppliers")
async def add_supplier(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    contact_person: str = Form(""),
    phone: str = Form(""),
    notes: str = Form(""),
    product_ids: list[str] = Form(default=[]),
):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        cur = conn.execute(
            """
            INSERT INTO suppliers (name, email, contact_person, phone, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                name.strip(),
                email.strip(),
                contact_person.strip(),
                phone.strip(),
                notes.strip(),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        sid = cur.lastrowid
        for pid in product_ids:
            if not pid:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO product_suppliers (product_id, supplier_id) VALUES (?, ?)",
                (int(pid), sid),
            )
        conn.commit()
        _flash(request, f"Поставщик «{name.strip()}» добавлен")
    except Exception as exc:
        _flash(request, str(exc), "err")
    finally:
        conn.close()
    return RedirectResponse("/suppliers", status_code=303)


@app.post("/suppliers/{supplier_id}")
async def edit_supplier(
    request: Request,
    supplier_id: int,
    name: str = Form(...),
    email: str = Form(...),
    contact_person: str = Form(""),
    phone: str = Form(""),
    notes: str = Form(""),
    product_ids: list[str] = Form(default=[]),
):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        conn.execute(
            """
            UPDATE suppliers
            SET name=?, email=?, contact_person=?, phone=?, notes=?
            WHERE id=?
            """,
            (
                name.strip(),
                email.strip(),
                contact_person.strip(),
                phone.strip(),
                notes.strip(),
                supplier_id,
            ),
        )
        conn.execute("DELETE FROM product_suppliers WHERE supplier_id=?", (supplier_id,))
        for pid in product_ids:
            if not pid:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO product_suppliers (product_id, supplier_id) VALUES (?, ?)",
                (int(pid), supplier_id),
            )
        conn.commit()
        _flash(request, "Поставщик обновлён")
    except Exception as exc:
        _flash(request, str(exc), "err")
    finally:
        conn.close()
    return RedirectResponse("/suppliers", status_code=303)


@app.post("/suppliers/{supplier_id}/delete")
async def delete_supplier(request: Request, supplier_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        conn.execute("DELETE FROM suppliers WHERE id=?", (supplier_id,))
        conn.commit()
        _flash(request, "Поставщик удалён")
    finally:
        conn.close()
    return RedirectResponse("/suppliers", status_code=303)


# ── RFQ ──────────────────────────────────────────────────


@app.get("/rfq", response_class=HTMLResponse)
async def rfq_list(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        rfqs = rows_to_dicts(
            conn.execute(
                """
                SELECT r.*,
                  (SELECT COUNT(*) FROM rfq_messages m WHERE m.rfq_id=r.id) AS msg_count,
                  (SELECT COUNT(*) FROM quote_replies qr
                     JOIN rfq_messages m ON m.id=qr.rfq_message_id
                     WHERE m.rfq_id=r.id) AS reply_count
                FROM rfqs r
                ORDER BY r.created_at DESC
                """
            ).fetchall()
        )
    finally:
        conn.close()
    return templates.TemplateResponse("rfq_list.html", _ctx(request, rfqs=rfqs))


@app.get("/rfq/new", response_class=HTMLResponse)
async def rfq_new(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        products = services.list_products(conn)
        suppliers = services.list_suppliers(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(
        "rfq_new.html",
        _ctx(request, products=products, suppliers=suppliers),
    )


@app.post("/rfq/new")
async def rfq_create(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    title = str(form.get("title") or "Запрос КП")
    notes = str(form.get("notes") or "")
    send_now = str(form.get("send_now") or "") == "1"

    product_ids = [int(x) for x in form.getlist("product_id") if str(x).isdigit()]
    quantities = form.getlist("quantity")
    units = form.getlist("unit")
    item_notes = form.getlist("item_note")
    supplier_ids = [int(x) for x in form.getlist("supplier_id") if str(x).isdigit()]

    if not product_ids:
        _flash(request, "Выберите хотя бы один товар", "err")
        return RedirectResponse("/rfq/new", status_code=303)
    if not supplier_ids:
        _flash(request, "Выберите хотя бы одного поставщика", "err")
        return RedirectResponse("/rfq/new", status_code=303)

    items = []
    for i, pid in enumerate(product_ids):
        qty = 1.0
        try:
            qty = float(quantities[i]) if i < len(quantities) else 1.0
        except (TypeError, ValueError, IndexError):
            qty = 1.0
        unit = units[i] if i < len(units) else "шт"
        note = item_notes[i] if i < len(item_notes) else ""
        items.append({"product_id": pid, "quantity": qty, "unit": unit, "note": note})

    conn = get_db()
    try:
        rfq_id = services.create_rfq(
            conn,
            title=title,
            notes=notes,
            items=items,
            supplier_ids=supplier_ids,
        )
        if send_now:
            result = services.send_rfq(conn, rfq_id)
            conn.commit()
            if result["errors"]:
                _flash(
                    request,
                    f"Отправлено {result['sent']}/{result['total']}. Ошибки: {'; '.join(result['errors'])}",
                    "err" if not result["sent"] else "ok",
                )
            else:
                _flash(request, f"Запрос отправлен {result['sent']} поставщикам")
        else:
            conn.commit()
            _flash(request, "Черновик запроса создан — можно отправить вручную")
    except Exception as exc:
        conn.rollback()
        _flash(request, str(exc), "err")
        return RedirectResponse("/rfq/new", status_code=303)
    finally:
        conn.close()
    return RedirectResponse(f"/rfq/{rfq_id}", status_code=303)


@app.get("/rfq/{rfq_id}", response_class=HTMLResponse)
async def rfq_view(request: Request, rfq_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        matrix = services.comparison_matrix(conn, rfq_id)
        if not matrix:
            return RedirectResponse("/rfq", status_code=303)
        chart_json = json.dumps(matrix.get("chart") or [], ensure_ascii=False)
    finally:
        conn.close()
    return templates.TemplateResponse(
        "rfq_detail.html",
        _ctx(request, matrix=matrix, chart_json=chart_json),
    )


@app.post("/rfq/{rfq_id}/send")
async def rfq_send(request: Request, rfq_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        result = services.send_rfq(conn, rfq_id)
        conn.commit()
        if result["errors"]:
            _flash(
                request,
                f"Отправлено {result['sent']}/{result['total']}. {'; '.join(result['errors'])}",
                "err" if not result["sent"] else "ok",
            )
        else:
            _flash(request, f"Отправлено писем: {result['sent']}")
    except Exception as exc:
        _flash(request, str(exc), "err")
    finally:
        conn.close()
    return RedirectResponse(f"/rfq/{rfq_id}", status_code=303)


@app.post("/rfq/{rfq_id}/poll")
async def rfq_poll(request: Request, rfq_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    from app.mail import poll_replies

    result = poll_replies(force_seen=True)
    if result.get("ok"):
        _flash(request, f"Проверено писем: {result.get('checked')}, новых ответов: {result.get('saved')}")
    else:
        _flash(request, result.get("error") or "Ошибка опроса почты", "err")
    return RedirectResponse(f"/rfq/{rfq_id}", status_code=303)


@app.post("/rfq/{rfq_id}/reparse/{reply_id}")
async def rfq_reparse(request: Request, rfq_id: int, reply_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    from app.parse_quotes import parse_and_store_reply

    conn = get_db()
    try:
        info = parse_and_store_reply(conn, reply_id)
        conn.commit()
        _flash(request, f"Переразобрано: {info['lines']} строк ({info['source']})")
    except Exception as exc:
        _flash(request, str(exc), "err")
    finally:
        conn.close()
    return RedirectResponse(f"/rfq/{rfq_id}", status_code=303)


# ── Settings / template ──────────────────────────────────


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        tpl = services.get_default_template(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(
        "settings.html",
        _ctx(
            request,
            tpl=tpl,
            mail_user=settings.mail_user or "(не задан)",
            mail_from=settings.mail_from_addr or "(не задан)",
            sender_name=settings.sender_name,
            sender_company=settings.sender_company,
        ),
    )


@app.post("/settings/template")
async def save_template(
    request: Request,
    subject_tpl: str = Form(...),
    body_tpl: str = Form(...),
):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM email_templates WHERE is_default=1 LIMIT 1").fetchone()
        if row:
            conn.execute(
                "UPDATE email_templates SET subject_tpl=?, body_tpl=? WHERE id=?",
                (subject_tpl, body_tpl, row["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO email_templates (name, subject_tpl, body_tpl, is_default)
                VALUES ('Стандартный', ?, ?, 1)
                """,
                (subject_tpl, body_tpl),
            )
        conn.commit()
        _flash(request, "Шаблон письма сохранён")
    finally:
        conn.close()
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/poll")
async def settings_poll(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    from app.mail import poll_replies

    result = poll_replies(force_seen=True)
    if result.get("ok"):
        _flash(request, f"Опрос: проверено {result.get('checked')}, сохранено {result.get('saved')}")
    else:
        _flash(request, result.get("error") or "Ошибка", "err")
    return RedirectResponse("/settings", status_code=303)
