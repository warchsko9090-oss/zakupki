from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db import ensure_seed_demo, get_db, init_db, rows_to_dicts
from app.scheduler import start_scheduler
from app import chats as chat_svc
from app import services
from app import files as files_svc

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
# Local edits to HTML should apply without full process restart
templates.env.auto_reload = True

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
    # Re-read flags so .env / AI / mail toggles apply without full restart
    from app.config import get_settings as _gs

    cfg = _gs()
    unread_rfq = 0
    unread_chats = 0
    try:
        conn = get_db()
        try:
            unread_rfq = services.count_unread_rfq_replies(conn)
            unread_chats = chat_svc.count_unread_chat_messages(conn)
        finally:
            conn.close()
    except Exception:
        pass
    return {
        "request": request,
        "app_name": cfg.app_name,
        "flash": _pop_flash(request),
        "mail_ready": cfg.mail_ready,
        "ai_ready": cfg.ai_ready,
        "nav_unread_rfq": unread_rfq,
        "nav_unread_chats": unread_chats,
        **extra,
    }


async def _read_uploads(form, field: str = "files") -> list[tuple[str, bytes, str]]:
    out: list[tuple[str, bytes, str]] = []
    for item in form.getlist(field):
        if not hasattr(item, "filename") or not item.filename:
            continue
        data = await item.read()
        if not data:
            continue
        mime = getattr(item, "content_type", None) or "application/octet-stream"
        out.append((str(item.filename), data, str(mime)))
    return out


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if require_auth(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", _ctx(request))


@app.post("/login")
async def login_post(request: Request, password: str = Form(...)):
    from app.config import get_settings as _gs

    _gs.cache_clear()
    expected = _gs().app_password
    if password == expected:
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
                """
                SELECT r.*,
                  (SELECT COUNT(*) FROM quote_replies qr
                     JOIN rfq_messages m ON m.id=qr.rfq_message_id
                     WHERE m.rfq_id=r.id AND qr.is_read=0) AS unread_count
                FROM rfqs r
                ORDER BY r.created_at DESC LIMIT 8
                """
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
    website: str = Form(""),
    notes: str = Form(""),
    product_ids: list[str] = Form(default=[]),
):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        cur = conn.execute(
            """
            INSERT INTO suppliers (name, email, contact_person, phone, website, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name.strip(),
                email.strip(),
                contact_person.strip(),
                phone.strip(),
                website.strip(),
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
    website: str = Form(""),
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
            SET name=?, email=?, contact_person=?, phone=?, website=?, notes=?
            WHERE id=?
            """,
            (
                name.strip(),
                email.strip(),
                contact_person.strip(),
                phone.strip(),
                website.strip(),
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
                     WHERE m.rfq_id=r.id) AS reply_count,
                  (SELECT COUNT(*) FROM quote_replies qr
                     JOIN rfq_messages m ON m.id=qr.rfq_message_id
                     WHERE m.rfq_id=r.id AND qr.is_read=0) AS unread_count
                FROM rfqs r
                ORDER BY r.created_at DESC
                """
            ).fetchall()
        )
        for r in rfqs:
            r["created_month_year"] = services.format_created_month_year(r.get("created_at"))
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
    letter_conditions = str(form.get("letter_conditions") or "")
    use_ai = str(form.get("use_ai") or "") == "1"
    ai_hint = str(form.get("ai_hint") or "")

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
        allowed = {s["id"] for s in services.suppliers_for_product_categories(conn, product_ids)}
        supplier_ids = [sid for sid in supplier_ids if sid in allowed]
        if not supplier_ids:
            _flash(
                request,
                "Выбранные поставщики не связаны с категориями выбранных товаров",
                "err",
            )
            return RedirectResponse("/rfq/new", status_code=303)
        rfq_id = services.create_rfq(
            conn,
            title=title,
            notes=notes,
            items=items,
            supplier_ids=supplier_ids,
            letter_conditions=letter_conditions,
        )
        if use_ai:
            result = services.ai_redraft_rfq(conn, rfq_id, extra_instruction=ai_hint)
            conn.commit()
            msg = (
                f"Черновик создан. AI учёл условия и слегка отредактировал: "
                f"{result['updated']}/{result['total']} — проверьте перед отправкой."
            )
            if result["errors"]:
                msg += " " + "; ".join(result["errors"])
                _flash(request, msg, "err" if not result["updated"] else "ok")
            else:
                _flash(request, msg)
        else:
            conn.commit()
            _flash(request, "Черновик создан — проверьте письма и отправьте")
    except Exception as exc:
        conn.rollback()
        _flash(request, str(exc), "err")
        return RedirectResponse("/rfq/new", status_code=303)
    finally:
        conn.close()
    return RedirectResponse(f"/rfq/{rfq_id}", status_code=303)


@app.post("/rfq/{rfq_id}/rename")
async def rfq_rename(request: Request, rfq_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    title = str(form.get("title") or "")
    nxt = str(form.get("next") or "/rfq")
    conn = get_db()
    try:
        services.rename_rfq(conn, rfq_id, title)
        conn.commit()
        _flash(request, "Название обновлено")
    except Exception as exc:
        conn.rollback()
        _flash(request, str(exc), "err")
    finally:
        conn.close()
    if nxt.startswith("/rfq"):
        return RedirectResponse(nxt, status_code=303)
    return RedirectResponse("/rfq", status_code=303)


@app.post("/rfq/{rfq_id}/delete")
async def rfq_delete(request: Request, rfq_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        services.delete_rfq(conn, rfq_id)
        conn.commit()
        _flash(request, "Запрос удалён")
    except Exception as exc:
        conn.rollback()
        _flash(request, str(exc), "err")
        return RedirectResponse(f"/rfq/{rfq_id}", status_code=303)
    finally:
        conn.close()
    return RedirectResponse("/rfq", status_code=303)


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
        company_accounts = chat_svc.list_company_accounts(conn)
        # Mark after loading so badges still show for this visit
        services.mark_rfq_replies_read(conn, rfq_id)
        conn.commit()
    finally:
        conn.close()
    return templates.TemplateResponse(
        "rfq_detail.html",
        _ctx(
            request,
            matrix=matrix,
            chart_json=chart_json,
            company_accounts=company_accounts,
        ),
    )


@app.post("/rfq/{rfq_id}/add-suppliers")
async def rfq_add_suppliers(request: Request, rfq_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    supplier_ids = [int(x) for x in form.getlist("supplier_id") if str(x).isdigit()]
    use_ai = str(form.get("use_ai") or "") == "1"
    ai_hint = str(form.get("ai_hint") or "")
    if not supplier_ids:
        _flash(request, "Выберите хотя бы одного поставщика", "err")
        return RedirectResponse(f"/rfq/{rfq_id}", status_code=303)
    conn = get_db()
    try:
        result = services.add_suppliers_to_rfq(
            conn,
            rfq_id,
            supplier_ids,
            use_ai=use_ai,
            ai_hint=ai_hint,
        )
        conn.commit()
        msg = f"Добавлено поставщиков: {result['added']}"
        if result["skipped"]:
            msg += f", пропущено: {result['skipped']}"
        if result.get("ai"):
            ai = result["ai"]
            msg += f". AI: {ai.get('updated', 0)}/{ai.get('total', 0)}"
            if ai.get("errors"):
                msg += " — " + "; ".join(ai["errors"])
        if result["added"]:
            msg += ". Проверьте черновики и нажмите «Отправить письма»."
            _flash(request, msg)
        else:
            _flash(request, msg or "Никого не добавили", "err")
    except Exception as exc:
        conn.rollback()
        _flash(request, str(exc), "err")
    finally:
        conn.close()
    return RedirectResponse(f"/rfq/{rfq_id}", status_code=303)


@app.post("/rfq/{rfq_id}/messages/{message_id}")
async def rfq_message_save(
    request: Request,
    rfq_id: int,
    message_id: int,
    subject: str = Form(...),
    body: str = Form(...),
):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        services.update_rfq_message(conn, message_id, subject=subject, body=body)
        conn.commit()
        _flash(request, "Письмо сохранено")
    except Exception as exc:
        _flash(request, str(exc), "err")
    finally:
        conn.close()
    return RedirectResponse(f"/rfq/{rfq_id}", status_code=303)


@app.post("/rfq/{rfq_id}/ai-draft")
async def rfq_ai_draft(
    request: Request,
    rfq_id: int,
    ai_hint: str = Form(""),
    message_id: str = Form(""),
):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    mid = int(message_id) if str(message_id).isdigit() else None
    conn = get_db()
    try:
        result = services.ai_redraft_rfq(
            conn, rfq_id, extra_instruction=ai_hint, message_id=mid
        )
        conn.commit()
        msg = f"AI обновил писем: {result['updated']}/{result['total']}. Проверьте текст перед отправкой."
        if result["errors"]:
            msg += " " + "; ".join(result["errors"])
            _flash(request, msg, "err" if not result["updated"] else "ok")
        else:
            _flash(request, msg)
    except Exception as exc:
        _flash(request, str(exc), "err")
    finally:
        conn.close()
    return RedirectResponse(f"/rfq/{rfq_id}", status_code=303)


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

    # Recent window is enough; full mailbox scan was hanging the app
    result = poll_replies(force_seen=True, max_messages=40, since_days=14)
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


@app.post("/rfq/{rfq_id}/import-reply/{message_id}")
async def rfq_import_reply(request: Request, rfq_id: int, message_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    body = str(form.get("body") or "")
    channel = str(form.get("channel") or "manual")
    update_conditions = str(form.get("update_conditions") or "") == "1"
    conn = get_db()
    try:
        result = services.import_pasted_reply(
            conn,
            rfq_id=rfq_id,
            message_id=message_id,
            body=body,
            channel=channel,
            update_conditions=update_conditions,
        )
        conn.commit()
        msg = (
            f"Ответ из {result.get('channel')} добавлен в аналитику: "
            f"{result.get('lines')} позиций ({result.get('source')})"
        )
        cond = result.get("conditions") or {}
        if cond.get("summary"):
            msg += f". Условия: {cond['summary']}"
        elif cond.get("error"):
            msg += f". Условия не обновлены: {cond['error']}"
        _flash(request, msg)
    except Exception as exc:
        conn.rollback()
        _flash(request, str(exc), "err")
    finally:
        conn.close()
    return RedirectResponse(f"/rfq/{rfq_id}", status_code=303)


@app.post("/rfq/{rfq_id}/followup/{message_id}")
async def rfq_followup(request: Request, rfq_id: int, message_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    body = str(form.get("body") or "")
    use_ai = str(form.get("use_ai") or "") == "1"
    to_email = str(form.get("to_email") or "")
    remember = str(form.get("remember_email") or "") == "1"
    company_raw = str(form.get("company_account_id") or "")
    company_id = int(company_raw) if company_raw.isdigit() else None
    uploads = await _read_uploads(form)
    conn = get_db()
    try:
        result = services.send_followup(
            conn,
            rfq_id=rfq_id,
            message_id=message_id,
            body=body,
            use_ai=use_ai,
            to_email=to_email,
            remember=remember,
            attachment_uploads=uploads,
            company_account_id=company_id,
        )
        conn.commit()
        to_addr = result.get("to_addr") or to_email
        _flash(
            request,
            f"Уточнение отправлено на {to_addr} — после ответа нажмите «Проверить почту»",
        )
    except Exception as exc:
        conn.rollback()
        _flash(request, str(exc), "err")
    finally:
        conn.close()
    return RedirectResponse(f"/rfq/{rfq_id}", status_code=303)


@app.post("/rfq/{rfq_id}/reply-email/{message_id}")
async def rfq_set_reply_email(request: Request, rfq_id: int, message_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    email = str(form.get("email") or "")
    update_supplier = str(form.get("update_supplier") or "") == "1"
    conn = get_db()
    try:
        result = services.set_thread_reply_email(
            conn,
            rfq_id=rfq_id,
            message_id=message_id,
            email=email,
            update_supplier=update_supplier,
        )
        conn.commit()
        msg = f"В этом чате отвечаем на {result['email']}"
        if result.get("supplier_updated"):
            msg += " (также обновлена карточка поставщика)"
        else:
            msg += f". В каталоге по-прежнему {result['catalog_email']}"
        _flash(request, msg)
    except Exception as exc:
        conn.rollback()
        _flash(request, str(exc), "err")
    finally:
        conn.close()
    return RedirectResponse(f"/rfq/{rfq_id}", status_code=303)


@app.post("/rfq/{rfq_id}/sync-conditions/{message_id}")
async def rfq_sync_conditions(request: Request, rfq_id: int, message_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        result = services.sync_conditions_from_thread(
            conn, rfq_id=rfq_id, message_id=message_id
        )
        conn.commit()
        msg = "Условия запроса обновлены из переписки"
        if result.get("summary"):
            msg += f": {result['summary']}"
        _flash(request, msg)
    except Exception as exc:
        conn.rollback()
        _flash(request, str(exc), "err")
    finally:
        conn.close()
    return RedirectResponse(f"/rfq/{rfq_id}", status_code=303)


# ── AI supplier search ───────────────────────────────────


@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    from app.supplier_search import list_sessions

    conn = get_db()
    try:
        products = services.list_products(conn)
        sessions = list_sessions(conn, limit=15)
    finally:
        conn.close()
    return templates.TemplateResponse(
        "search.html",
        _ctx(request, products=products, sessions=sessions),
    )


@app.post("/search")
async def search_run(
    request: Request,
    query: str = Form(...),
    product_id: str = Form(""),
):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    from app.supplier_search import run_supplier_search

    pid = int(product_id) if str(product_id).isdigit() else None
    conn = get_db()
    try:
        result = run_supplier_search(conn, query=query, product_id=pid)
        _flash(request, f"Готово: {result['findings']} карточек (ссылок: {result['hits']})")
        sid = result["session_id"]
    except Exception as exc:
        _flash(request, str(exc), "err")
        return RedirectResponse("/search", status_code=303)
    finally:
        conn.close()
    return RedirectResponse(f"/search/{sid}", status_code=303)


@app.get("/search/{session_id}", response_class=HTMLResponse)
async def search_view(request: Request, session_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    from app.supplier_search import get_session

    conn = get_db()
    try:
        session = get_session(conn, session_id)
        products = services.list_products(conn)
        if not session:
            return RedirectResponse("/search", status_code=303)
    finally:
        conn.close()
    return templates.TemplateResponse(
        "search_result.html",
        _ctx(request, session=session, products=products),
    )


@app.post("/search/findings/{finding_id}/add")
async def search_add_finding(
    request: Request,
    finding_id: int,
    product_ids: list[str] = Form(default=[]),
):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    from app.supplier_search import add_finding_as_supplier

    form = await request.form()
    session_id = str(form.get("session_id") or "")
    pids = [int(x) for x in product_ids if str(x).isdigit()]
    conn = get_db()
    try:
        result = add_finding_as_supplier(conn, finding_id=finding_id, product_ids=pids)
        conn.commit()
        linked = int(result.get("linked_products") or 0)
        if result.get("created"):
            msg = f"Поставщик «{result['name']}» добавлен в базу"
            if linked:
                msg += f", товаров привязано: {linked}"
            else:
                msg += " — без товаров не попадёт в запросы по категориям"
            _flash(request, msg, "ok" if linked else "err")
        else:
            msg = f"«{result['name']}» уже в базе"
            if linked:
                msg += f" — привязка к товарам обновлена ({linked})"
            else:
                msg += " — отметьте товары номенклатуры, иначе в запрос не попадёт"
            _flash(request, msg, "ok" if linked else "err")
    except Exception as exc:
        _flash(request, str(exc), "err")
    finally:
        conn.close()
    if session_id.isdigit():
        return RedirectResponse(f"/search/{session_id}", status_code=303)
    return RedirectResponse("/search", status_code=303)


# ── Supplier chats ───────────────────────────────────────


@app.get("/chats", response_class=HTMLResponse)
async def chats_list(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        chats = chat_svc.list_chats(conn)
    finally:
        conn.close()
    return templates.TemplateResponse("chats_list.html", _ctx(request, chats=chats))


@app.get("/chats/new", response_class=HTMLResponse)
async def chats_new(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        suppliers = services.list_suppliers(conn)
        products = services.list_products(conn)
    finally:
        conn.close()
    return templates.TemplateResponse(
        "chats_new.html",
        _ctx(request, suppliers=suppliers, products=products),
    )


def _parse_chat_items_from_form(form) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    product_ids = [int(x) for x in form.getlist("product_id") if str(x).isdigit()]
    quantities = form.getlist("quantity")
    units = form.getlist("unit")
    item_notes = form.getlist("item_note")
    for i, pid in enumerate(product_ids):
        qty = 1.0
        try:
            qty = float(quantities[i]) if i < len(quantities) else 1.0
        except (TypeError, ValueError, IndexError):
            qty = 1.0
        items.append(
            {
                "product_id": pid,
                "custom_name": "",
                "quantity": qty,
                "unit": units[i] if i < len(units) else "шт",
                "note": item_notes[i] if i < len(item_notes) else "",
            }
        )
    free_names = form.getlist("free_name")
    free_qtys = form.getlist("free_qty")
    free_units = form.getlist("free_unit")
    free_notes = form.getlist("free_note")
    for i, name in enumerate(free_names):
        name = str(name or "").strip()
        if not name:
            continue
        qty = 1.0
        try:
            qty = float(free_qtys[i]) if i < len(free_qtys) else 1.0
        except (TypeError, ValueError, IndexError):
            qty = 1.0
        items.append(
            {
                "product_id": None,
                "custom_name": name,
                "quantity": qty,
                "unit": free_units[i] if i < len(free_units) else "шт",
                "note": free_notes[i] if i < len(free_notes) else "",
            }
        )
    return items


@app.post("/chats/preview", response_class=HTMLResponse)
async def chats_preview(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    supplier_raw = str(form.get("supplier_id") or "")
    if not supplier_raw.isdigit():
        _flash(request, "Выберите поставщика", "err")
        return RedirectResponse("/chats/new", status_code=303)
    items = _parse_chat_items_from_form(form)
    title = str(form.get("title") or "")
    notes = str(form.get("notes") or "")
    conditions = str(form.get("letter_conditions") or "")
    ai_hint = str(form.get("ai_hint") or "")
    conn = get_db()
    try:
        supplier = conn.execute(
            "SELECT * FROM suppliers WHERE id=?", (int(supplier_raw),)
        ).fetchone()
        if not supplier:
            _flash(request, "Поставщик не найден", "err")
            return RedirectResponse("/chats/new", status_code=303)
        supplier_d = dict(supplier)
        for it in items:
            if it.get("product_id") and not it.get("custom_name"):
                p = conn.execute(
                    "SELECT name FROM products WHERE id=?", (int(it["product_id"]),)
                ).fetchone()
                if p:
                    it["custom_name"] = p["name"]
                    it["name"] = p["name"]
        letter = chat_svc.ai_prepare_first_letter(
            conn,
            supplier_name=supplier_d["name"],
            contact=supplier_d.get("contact_person") or "",
            items=items,
            conditions=conditions,
            extra_hint=ai_hint,
        )
    finally:
        conn.close()
    # Draft lives in the form (not cookie session) — long letters exceed session cookie size
    items_json = json.dumps(items, ensure_ascii=False)
    return templates.TemplateResponse(
        "chats_preview.html",
        _ctx(
            request,
            supplier=supplier_d,
            title=title,
            notes=notes,
            items=items,
            items_json=items_json,
            letter_conditions=conditions,
            subject=letter["subject"],
            body=letter["body"],
            letter_source=letter.get("source") or "template",
        ),
    )


@app.post("/chats/confirm")
async def chats_confirm(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    supplier_raw = str(form.get("supplier_id") or "")
    if not supplier_raw.isdigit():
        _flash(request, "Выберите поставщика", "err")
        return RedirectResponse("/chats/new", status_code=303)
    try:
        items = json.loads(str(form.get("items_json") or "[]"))
        if not isinstance(items, list):
            items = []
    except json.JSONDecodeError:
        items = []
    title = str(form.get("title") or "")
    notes = str(form.get("notes") or "")
    conditions = str(form.get("letter_conditions") or "")
    subject = str(form.get("subject") or "")
    body = str(form.get("body") or "")
    send_now = str(form.get("send_now") or "") == "1"
    if not subject.strip() or not body.strip():
        _flash(request, "Заполните тему и текст письма", "err")
        return RedirectResponse("/chats/new", status_code=303)
    conn = get_db()
    try:
        chat_id = chat_svc.create_chat(
            conn,
            supplier_id=int(supplier_raw),
            title=title,
            notes=notes,
            letter_conditions=conditions,
            items=items,
        )
        chat = chat_svc.get_chat(conn, chat_id)
        token_mark = f"[CHAT-{chat['token']}]"
        subject = subject.replace("[CHAT-XXXX]", token_mark)
        body = body.replace("[CHAT-XXXX]", token_mark)
        if token_mark not in subject:
            subject = f"{subject} {token_mark}".strip()
        if send_now:
            chat_svc.send_chat_message(
                conn,
                chat_id=chat_id,
                body=body,
                subject=subject,
                to_email=chat["active_email"],
            )
            _flash(request, "Чат создан, первое письмо отправлено")
        else:
            conn.execute(
                """
                INSERT INTO supplier_chat_messages
                  (chat_id, direction, kind, subject, body, to_addr, status, created_at)
                VALUES (?, 'out', 'draft', ?, ?, ?, 'pending', ?)
                """,
                (
                    chat_id,
                    subject,
                    body,
                    chat["active_email"],
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.execute(
                "UPDATE supplier_chats SET updated_at=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), chat_id),
            )
            _flash(request, "Чат создан — проверьте черновик и отправьте")
        conn.commit()
    except Exception as exc:
        conn.rollback()
        _flash(request, str(exc), "err")
        return RedirectResponse("/chats/new", status_code=303)
    finally:
        conn.close()
    return RedirectResponse(f"/chats/{chat_id}", status_code=303)


@app.post("/chats/new")
async def chats_create_legacy(request: Request):
    """Backward-compatible: redirect old POST to preview flow."""
    return await chats_preview(request)


@app.post("/chats/{chat_id}/reparse/{message_id}")
async def chat_reparse(request: Request, chat_id: int, message_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    from app.parse_quotes import parse_and_store_chat_message

    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id FROM supplier_chat_messages WHERE id=? AND chat_id=? AND direction='in'",
            (message_id, chat_id),
        ).fetchone()
        if not row:
            raise ValueError("Сообщение не найдено")
        result = parse_and_store_chat_message(conn, message_id)
        conn.commit()
        _flash(request, f"Разбор: {result.get('source')}, строк: {result.get('lines')}")
    except Exception as exc:
        conn.rollback()
        _flash(request, str(exc), "err")
    finally:
        conn.close()
    return RedirectResponse(f"/chats/{chat_id}", status_code=303)


@app.post("/chats/{chat_id}/send-draft/{message_id}")
async def chat_send_draft(request: Request, chat_id: int, message_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT * FROM supplier_chat_messages
            WHERE id=? AND chat_id=? AND direction='out' AND status IN ('pending','error')
            """,
            (message_id, chat_id),
        ).fetchone()
        if not row:
            raise ValueError("Черновик не найден")
        chat_svc.send_chat_message(
            conn,
            chat_id=chat_id,
            body=row["body"] or "",
            subject=row["subject"] or "",
            to_email=row["to_addr"] or "",
        )
        # remove old pending draft to avoid duplicates
        conn.execute("DELETE FROM supplier_chat_messages WHERE id=?", (message_id,))
        conn.commit()
        _flash(request, "Черновик отправлен")
    except Exception as exc:
        conn.rollback()
        _flash(request, str(exc), "err")
    finally:
        conn.close()
    return RedirectResponse(f"/chats/{chat_id}", status_code=303)


@app.get("/chats/{chat_id}", response_class=HTMLResponse)
async def chat_view(request: Request, chat_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        chat = chat_svc.get_chat(conn, chat_id)
        if not chat:
            return RedirectResponse("/chats", status_code=303)
        accounts = chat_svc.list_company_accounts(conn)
        draft_body = ""
        if str(request.query_params.get("draft") or "") == "1":
            draft_body = chat_svc.draft_opening_message(conn, chat_id)
        blocks = {str(a["id"]): chat_svc.format_company_requisites(a) for a in accounts}
        blocks_json = json.dumps(blocks, ensure_ascii=False)
        chat_svc.mark_chat_messages_read(conn, chat_id)
        conn.commit()
    finally:
        conn.close()
    return templates.TemplateResponse(
        "chats_detail.html",
        _ctx(
            request,
            chat=chat,
            company_accounts=accounts,
            draft_body=draft_body,
            requisite_blocks_json=blocks_json,
        ),
    )


@app.post("/chats/{chat_id}/send")
async def chat_send(request: Request, chat_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    uploads = await _read_uploads(form)
    company_raw = str(form.get("company_account_id") or "")
    company_id = int(company_raw) if company_raw.isdigit() else None
    conn = get_db()
    try:
        result = chat_svc.send_chat_message(
            conn,
            chat_id=chat_id,
            body=str(form.get("body") or ""),
            subject=str(form.get("subject") or ""),
            to_email=str(form.get("to_email") or ""),
            remember_email=str(form.get("remember_email") or "") == "1",
            attachment_uploads=uploads,
            company_account_id=company_id,
        )
        conn.commit()
        _flash(request, f"Отправлено на {result.get('to_addr')}")
    except Exception as exc:
        conn.rollback()
        _flash(request, str(exc), "err")
    finally:
        conn.close()
    return RedirectResponse(f"/chats/{chat_id}", status_code=303)


@app.post("/chats/{chat_id}/import")
async def chat_import(request: Request, chat_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    uploads = await _read_uploads(form)
    conn = get_db()
    try:
        chat_svc.import_chat_inbound(
            conn,
            chat_id=chat_id,
            body=str(form.get("body") or ""),
            channel=str(form.get("channel") or "manual"),
            attachment_uploads=uploads,
        )
        conn.commit()
        _flash(request, "Входящее добавлено")
    except Exception as exc:
        conn.rollback()
        _flash(request, str(exc), "err")
    finally:
        conn.close()
    return RedirectResponse(f"/chats/{chat_id}", status_code=303)


@app.post("/chats/{chat_id}/poll")
async def chat_poll(request: Request, chat_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    from app.mail import poll_replies

    try:
        result = poll_replies(force_seen=True, since_days=21)
        if result.get("ok"):
            _flash(
                request,
                f"Почта: просмотрено {result.get('checked', 0)}, новых {result.get('saved', 0)}",
            )
        else:
            _flash(request, result.get("error") or "Ошибка почты", "err")
    except Exception as exc:
        _flash(request, str(exc), "err")
    return RedirectResponse(f"/chats/{chat_id}", status_code=303)


@app.post("/chats/{chat_id}/delete")
async def chat_delete(request: Request, chat_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        chat_svc.delete_chat(conn, chat_id)
        conn.commit()
        _flash(request, "Чат удалён")
    except Exception as exc:
        conn.rollback()
        _flash(request, str(exc), "err")
        return RedirectResponse(f"/chats/{chat_id}", status_code=303)
    finally:
        conn.close()
    return RedirectResponse("/chats", status_code=303)


@app.get("/files/{file_id}")
async def file_download(request: Request, file_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        meta = files_svc.get_file(conn, file_id)
    finally:
        conn.close()
    if not meta:
        return RedirectResponse("/", status_code=303)
    path = files_svc.resolve_path(meta["stored_name"])
    if not path.is_file():
        _flash(request, "Файл не найден на диске", "err")
        return RedirectResponse("/", status_code=303)
    return FileResponse(
        path,
        filename=meta["filename"],
        media_type=meta.get("mime") or "application/octet-stream",
    )


@app.post("/files/{file_id}/kind")
async def file_set_kind(request: Request, file_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    nxt = str(form.get("next") or "/")
    conn = get_db()
    try:
        chat_svc.set_file_kind(conn, file_id, str(form.get("kind") or "other"))
        conn.commit()
        _flash(request, "Тип файла обновлён")
    except Exception as exc:
        conn.rollback()
        _flash(request, str(exc), "err")
    finally:
        conn.close()
    if nxt.startswith("/"):
        return RedirectResponse(nxt, status_code=303)
    return RedirectResponse("/", status_code=303)


# ── Settings / template ──────────────────────────────────


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    from app.config import get_settings as _gs

    _gs.cache_clear()
    cfg = _gs()
    init_db()
    conn = get_db()
    try:
        tpl = services.get_default_template(conn)
        profile = services.get_manager_profile(conn)
        company_accounts = chat_svc.list_company_accounts(conn)
        att_dir = cfg.attachments_dir
        db_file_rows = conn.execute("SELECT COUNT(*) AS c FROM message_files").fetchone()["c"]
        storage = {
            "data_dir": str(cfg.data_root),
            "db_path": str(cfg.db_path),
            "db_exists": cfg.db_path.is_file(),
            "attachments_dir": str(att_dir),
            "attachments_count": len([p for p in att_dir.iterdir() if p.is_file()]) if att_dir.is_dir() else 0,
            "db_file_rows": int(db_file_rows or 0),
        }
    finally:
        conn.close()
    from app.scheduler import get_last_poll

    return templates.TemplateResponse(
        "settings.html",
        _ctx(
            request,
            tpl=tpl,
            profile=profile,
            company_accounts=company_accounts,
            storage=storage,
            mail_user=cfg.mail_user or "(не задан)",
            mail_from=cfg.mail_from_addr or "(не задан)",
            mail_pw_len=len(cfg.mail_password or ""),
            mail_test=request.session.pop("mail_test", None),
            mail_poll_enabled=cfg.mail_poll_enabled,
            mail_poll_minutes=cfg.mail_poll_minutes,
            last_poll=get_last_poll(),
        ),
    )


@app.post("/settings/companies")
async def company_create(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    conn = get_db()
    try:
        chat_svc.save_company_account(
            conn,
            {
                "label": form.get("label"),
                "legal_name": form.get("legal_name"),
                "inn": form.get("inn"),
                "kpp": form.get("kpp"),
                "bank_name": form.get("bank_name"),
                "bik": form.get("bik"),
                "checking_account": form.get("checking_account"),
                "corr_account": form.get("corr_account"),
                "address": form.get("address"),
                "is_default": str(form.get("is_default") or "") == "1",
            },
        )
        conn.commit()
        _flash(request, "Компания добавлена")
    except Exception as exc:
        conn.rollback()
        _flash(request, str(exc), "err")
    finally:
        conn.close()
    return RedirectResponse("/settings#companies", status_code=303)


@app.post("/settings/companies/{account_id}")
async def company_update(request: Request, account_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    conn = get_db()
    try:
        chat_svc.save_company_account(
            conn,
            {
                "label": form.get("label"),
                "legal_name": form.get("legal_name"),
                "inn": form.get("inn"),
                "kpp": form.get("kpp"),
                "bank_name": form.get("bank_name"),
                "bik": form.get("bik"),
                "checking_account": form.get("checking_account"),
                "corr_account": form.get("corr_account"),
                "address": form.get("address"),
                "is_default": str(form.get("is_default") or "") == "1",
            },
            account_id=account_id,
        )
        conn.commit()
        _flash(request, "Реквизиты сохранены")
    except Exception as exc:
        conn.rollback()
        _flash(request, str(exc), "err")
    finally:
        conn.close()
    return RedirectResponse("/settings#companies", status_code=303)


@app.post("/settings/companies/{account_id}/delete")
async def company_delete(request: Request, account_id: int):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        chat_svc.delete_company_account(conn, account_id)
        conn.commit()
        _flash(request, "Реквизиты удалены")
    except Exception as exc:
        conn.rollback()
        _flash(request, str(exc), "err")
    finally:
        conn.close()
    return RedirectResponse("/settings#companies", status_code=303)


@app.post("/settings/signature")
async def save_signature(
    request: Request,
    sender_name: str = Form(...),
    sender_company: str = Form(""),
    sender_position: str = Form(""),
    sender_phone: str = Form(""),
    sender_website: str = Form(""),
    signature_note: str = Form(""),
):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    conn = get_db()
    try:
        services.save_manager_profile(
            conn,
            sender_name=sender_name,
            sender_company=sender_company,
            sender_position=sender_position,
            sender_phone=sender_phone,
            sender_website=sender_website,
            signature_note=signature_note,
        )
        conn.commit()
        _flash(request, "Подпись менеджера сохранена")
    finally:
        conn.close()
    return RedirectResponse("/settings#signature", status_code=303)


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
                VALUES ('Стандартный запрос КП', ?, ?, 1)
                """,
                (subject_tpl, body_tpl),
            )
        conn.commit()
        _flash(request, "Шаблон письма сохранён")
    finally:
        conn.close()
    return RedirectResponse("/settings#template", status_code=303)


@app.post("/settings/template/reset")
async def reset_template(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    from app.db import DEFAULT_TEMPLATE_BODY, DEFAULT_TEMPLATE_SUBJECT

    conn = get_db()
    try:
        row = conn.execute("SELECT id FROM email_templates WHERE is_default=1 LIMIT 1").fetchone()
        if row:
            conn.execute(
                "UPDATE email_templates SET name=?, subject_tpl=?, body_tpl=? WHERE id=?",
                ("Стандартный запрос КП", DEFAULT_TEMPLATE_SUBJECT, DEFAULT_TEMPLATE_BODY, row["id"]),
            )
        else:
            conn.execute(
                """
                INSERT INTO email_templates (name, subject_tpl, body_tpl, is_default)
                VALUES (?, ?, ?, 1)
                """,
                ("Стандартный запрос КП", DEFAULT_TEMPLATE_SUBJECT, DEFAULT_TEMPLATE_BODY),
            )
        conn.commit()
        _flash(request, "Шаблон сброшен к стандартному запросу закупщика")
    finally:
        conn.close()
    return RedirectResponse("/settings#template", status_code=303)


@app.post("/settings/test-mail")
@app.post("/settings/mail-test")
async def settings_mail_test(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    from app.mail import test_mail_login

    try:
        result = test_mail_login()
    except Exception as exc:
        result = {"ok": False, "error": f"Сбой проверки: {exc}"}
    request.session["mail_test"] = result
    if result.get("ok"):
        _flash(request, f"Почта работает: {result.get('detail')}")
    else:
        _flash(request, result.get("error") or "Ошибка входа в почту", "err")
    return RedirectResponse("/settings", status_code=303)


@app.get("/settings/test-mail")
@app.get("/settings/mail-test")
async def settings_mail_test_get(request: Request):
    """Avoid bare Not Found if browser re-POSTs as GET after restart."""
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    _flash(request, "Нажмите кнопку «Проверить вход» ещё раз (нужен POST).", "err")
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/poll")
async def settings_poll(request: Request):
    if not require_auth(request):
        return RedirectResponse("/login", status_code=303)
    from app.mail import poll_replies

    result = poll_replies(force_seen=False)
    if result.get("ok"):
        _flash(request, f"Опрос: проверено {result.get('checked')}, сохранено {result.get('saved')}")
    else:
        _flash(request, result.get("error") or "Ошибка", "err")
    return RedirectResponse("/settings", status_code=303)
