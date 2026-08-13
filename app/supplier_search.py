"""Supplier discovery: web search + page fetch + AI-controlled extraction."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from html import unescape
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from app.ai_client import AiError, chat_json, _detect_proxy
from app.db import rows_to_dicts

log = logging.getLogger("supplier_search")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Domains that rarely help as "suppliers" for RFQ (keep Avito — sometimes useful)
NOISE_HOST_PARTS = (
    "wikipedia.org",
    "youtube.com",
    "vk.com",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "ozon.ru",
    "ozon.by",
    "ozon.",
    "wildberries.ru",
    "market.yandex.ru",
    "aliexpress.",
    "holodilnik.ru",
    "dns-shop.ru",
    "mvideo.ru",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _http_client(timeout: float = 25.0) -> httpx.Client:
    proxy = _detect_proxy()
    headers = {"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"}
    if proxy:
        try:
            return httpx.Client(
                timeout=timeout, trust_env=False, follow_redirects=True, headers=headers, proxy=proxy
            )
        except Exception as exc:
            log.warning("Proxy %s unavailable for search (%s)", proxy, exc)
    return httpx.Client(timeout=timeout, trust_env=False, follow_redirects=True, headers=headers)


def _host(url: str) -> str:
    try:
        h = (urlparse(url).netloc or "").lower()
    except Exception:
        return ""
    return h[4:] if h.startswith("www.") else h


def _is_noise_url(url: str) -> bool:
    h = _host(url)
    return any(n in h for n in NOISE_HOST_PARTS)


def _ddgs_results(query: str, limit: int = 10) -> list[dict[str, str]]:
    """Primary search via ddgs (works behind SOCKS when HTML DDG is blocked)."""
    results: list[dict[str, str]] = []
    try:
        from ddgs import DDGS
    except ImportError:
        log.warning("ddgs not installed")
        return results

    proxy = _detect_proxy()
    try:
        kwargs = {"proxy": proxy} if proxy else {}
        # Avoid Windows socks4:// registry proxy breaking httpx inside ddgs
        ddg = DDGS(**kwargs) if kwargs else DDGS(proxy=None)
        # DDGS may still read system proxies; force socks5 via env for this call
        import os

        old = {k: os.environ.get(k) for k in ("ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "http_proxy", "https_proxy")}
        try:
            if proxy:
                for k in list(old):
                    os.environ[k] = proxy
            else:
                for k in list(old):
                    os.environ.pop(k, None)
            raw = list(ddg.text(query, region="ru-ru", max_results=max(limit, 8)))
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    except Exception as exc:
        log.warning("ddgs search failed for %r: %s", query, exc)
        return results

    for item in raw:
        url = (item.get("href") or item.get("url") or "").strip()
        if not url.startswith("http") or _is_noise_url(url):
            continue
        results.append(
            {
                "url": url,
                "title": str(item.get("title") or "")[:200],
                "snippet": str(item.get("body") or item.get("description") or "")[:500],
                "engine": "ddgs",
            }
        )
        if len(results) >= limit:
            break
    return results


def _ddg_html_results(query: str, limit: int = 8) -> list[dict[str, str]]:
    """Legacy DuckDuckGo HTML (often blocked with HTTP 202)."""
    results: list[dict[str, str]] = []
    url = "https://html.duckduckgo.com/html/"
    try:
        with _http_client(25.0) as client:
            r = client.post(url, data={"q": query})
            html = r.text
    except Exception as exc:
        log.warning("DDG HTML search failed: %s", exc)
        return results

    for m in re.finditer(
        r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</(?:a|td|div)',
        html,
        re.I | re.S,
    ):
        href, title, snippet = m.group(1), _strip_html(m.group(2)), _strip_html(m.group(3))
        real = _unwrap_ddg(href)
        if not real or "duckduckgo.com" in real or _is_noise_url(real):
            continue
        results.append({"url": real, "title": title[:200], "snippet": snippet[:400], "engine": "ddg-html"})
        if len(results) >= limit:
            break

    if not results:
        for m in re.finditer(r'uddg=([^&"]+)', html):
            real = unquote(m.group(1))
            if real.startswith("http") and "duckduckgo.com" not in real and not _is_noise_url(real):
                results.append({"url": real, "title": real, "snippet": "", "engine": "ddg-html"})
                if len(results) >= limit:
                    break
    return results


def _unwrap_ddg(href: str) -> str:
    if "uddg=" in href:
        qs = parse_qs(urlparse(href).query)
        if "uddg" in qs:
            return unquote(qs["uddg"][0])
    if href.startswith("//"):
        return "https:" + href
    return href


def web_search(query: str, limit: int = 10) -> list[dict[str, str]]:
    """Multi-backend web search; prefer ddgs."""
    hits = _ddgs_results(query, limit=limit)
    if len(hits) < max(3, limit // 2):
        for h in _ddg_html_results(query, limit=limit):
            if all(h["url"] != x["url"] for x in hits):
                hits.append(h)
            if len(hits) >= limit:
                break
    return hits[:limit]


def _fetch_page(url: str, max_chars: int = 8000) -> str:
    try:
        with _http_client(20.0) as client:
            r = client.get(url)
            if r.status_code >= 400:
                return ""
            ctype = (r.headers.get("content-type") or "").lower()
            if "html" not in ctype and "text" not in ctype and ctype:
                return ""
            return _strip_html(r.text)[:max_chars]
    except Exception as exc:
        log.info("Fetch failed %s: %s", url, exc)
        return ""


def _fallback_queries(q: str) -> list[str]:
    base = re.sub(r"\s+", " ", (q or "").strip())
    variants = [
        base,
        f"{base} купить поставщик",
        f"{base} производитель Россия",
        f"{base} оптом дистрибьютор",
        f"{base} цена сайт",
    ]
    words = base.split()
    if len(words) > 8:
        short = " ".join(words[:8])
        variants.append(f"{short} поставщик купить")
        variants.append(f"{short} производитель оптом")
    out: list[str] = []
    seen = set()
    for v in variants:
        v = v.strip()
        if v and v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    return out[:6]


def _normalize_plan(plan: Any, fallback_queries: list[str], focus_default: str) -> dict[str, Any]:
    """Normalize AI search plan: queries + sphere include/exclude."""
    queries = list(fallback_queries)
    focus = focus_default
    sphere = ""
    include: list[str] = []
    exclude: list[str] = []
    if isinstance(plan, dict):
        if plan.get("focus"):
            focus = str(plan.get("focus") or focus_default).strip() or focus_default
        sphere = str(plan.get("sphere") or "").strip()
        for key, bucket in (("sphere_include", include), ("sphere_exclude", exclude)):
            raw = plan.get(key)
            if isinstance(raw, list):
                for item in raw:
                    s = str(item).strip()
                    if s and s not in bucket:
                        bucket.append(s)
            elif isinstance(raw, str) and raw.strip():
                bucket.append(raw.strip())
        planned = plan.get("search_queries")
        if isinstance(planned, list) and planned:
            merged: list[str] = []
            seen_q: set[str] = set()
            for item in list(planned) + fallback_queries:
                s = str(item).strip()
                if s and s.lower() not in seen_q:
                    seen_q.add(s.lower())
                    merged.append(s)
            queries = merged[:10]
    return {
        "queries": queries,
        "focus": focus,
        "sphere": sphere,
        "sphere_include": include[:8],
        "sphere_exclude": exclude[:10],
    }


def run_supplier_search(conn, *, query: str, product_id: int | None = None) -> dict[str, Any]:
    """
    Manager chat-like query → search session with structured findings.
    AI controls extraction; web search+fetch supplies evidence.
    Broadens to the same commercial sphere (makers/distributors), not only the SKU.
    """
    q = (query or "").strip()
    if not q:
        raise ValueError("Пустой запрос")

    product_hint = ""
    if product_id:
        row = conn.execute(
            """
            SELECT p.name, p.unit, c.name AS category_name
            FROM products p JOIN categories c ON c.id=p.category_id
            WHERE p.id=?
            """,
            (product_id,),
        ).fetchone()
        if row:
            product_hint = f"Номенклатура: {row['category_name']} / {row['name']} ({row['unit']})"

    cur = conn.execute(
        """
        INSERT INTO search_sessions (query, product_id, status, summary, created_at)
        VALUES (?, ?, 'running', '', ?)
        """,
        (q, product_id, _now()),
    )
    session_id = int(cur.lastrowid)
    conn.commit()

    try:
        fallback = _fallback_queries(q)
        plan_data = _normalize_plan(None, fallback, q)
        try:
            plan = chat_json(
                system=(
                    "Ты ассистент отдела закупок РФ. По запросу менеджера верни JSON-объект:\n"
                    "{"
                    '"focus":"кратко что ищем",'
                    '"sphere":"коммерческая сфера одной фразой (узкая ниша рынка)",'
                    '"sphere_include":["кого включать: производители/дистрибьюторы/опт той же сферы"],'
                    '"sphere_exclude":["смежные, но ДРУГИЕ сферы — не путать"],'
                    '"search_queries":["..."]'
                    "}.\n"
                    "Правила сферы:\n"
                    "- Ищем не только точный SKU, а всю ту же сферу: производители, дистрибьюторы, "
                    "оптовые и прямые поставщики этой ниши.\n"
                    "- Сфера должна быть ОДНОЙ. Пример: монофосфат калия → "
                    "«минеральные/агрохимические удобрения для растений (растениеводство)»; "
                    "НЕ кормовые добавки, НЕ удобрения/премиксы для скота, НЕ ветеринария.\n"
                    "- sphere_exclude — явный список «ложных соседей» (животноводство, кормовые, "
                    "бытовая химия, нерелевантный ритейл и т.п. по смыслу запроса).\n"
                    "search_queries — 8–10 коротких русскоязычных запросов для поиска:\n"
                    "1) точный товар + купить/поставщик/производитель/опт/цена;\n"
                    "2) запросы по сфере целиком: «производители …», «дистрибьюторы …», "
                    "«оптовые поставщики …», каталоги/заводы в РФ;\n"
                    "3) варьируй формулировки, держи маркеры сферы (для растений / агро / "
                    "растениеводство и т.п.), чтобы не утянуть чужую нишу.\n"
                    "Без markdown."
                ),
                user=f"Запрос менеджера:\n{q}\n{product_hint}",
                temperature=0.35,
                timeout=60.0,
            )
            plan_data = _normalize_plan(plan, fallback, q)
        except Exception as exc:
            log.warning("AI query planning failed, using fallbacks: %s", exc)

        queries = plan_data["queries"]
        focus = plan_data["focus"]
        sphere = plan_data["sphere"]
        sphere_include = plan_data["sphere_include"]
        sphere_exclude = plan_data["sphere_exclude"]

        hits: list[dict[str, str]] = []
        seen_url: set[str] = set()
        host_count: dict[str, int] = {}
        for sq in queries:
            for hit in web_search(str(sq), limit=8):
                u = hit["url"]
                host = _host(u)
                if u in seen_url:
                    continue
                if host and host_count.get(host, 0) >= 2:
                    continue
                seen_url.add(u)
                if host:
                    host_count[host] = host_count.get(host, 0) + 1
                hit = dict(hit)
                hit["query"] = str(sq)
                hits.append(hit)
            if len(hits) >= 28:
                break

        pages: list[dict[str, str]] = []
        for hit in hits[:16]:
            body = _fetch_page(hit["url"])
            pages.append(
                {
                    "url": hit["url"],
                    "title": hit.get("title") or "",
                    "snippet": hit.get("snippet") or "",
                    "text": body[:6000],
                    "query": hit.get("query") or "",
                }
            )

        evidence = []
        for p in pages:
            evidence.append(
                {
                    "url": p["url"],
                    "title": p["title"],
                    "snippet": p["snippet"],
                    "excerpt": (p["text"] or p["snippet"] or "")[:3500],
                }
            )

        findings: list[Any] = []
        summary = ""
        if evidence:
            sphere_block = (
                f"Сфера (одна ниша): {sphere or focus}\n"
                f"Включать роли/типы: {sphere_include or ['производители', 'дистрибьюторы', 'опт той же сферы']}\n"
                f"Исключать смежные сферы: {sphere_exclude or ['всё вне указанной сферы']}\n"
            )
            extracted = chat_json(
                system=(
                    "Ты контролёр результатов поиска поставщиков для отдела закупок РФ. "
                    "Используй данные из evidence (title/snippet/excerpt). "
                    "Не выдумывай email/телефон/цену — если нет в evidence, оставь пустую строку. "
                    "ШИРОТА: включай компании той же коммерческой сферы — производителей, "
                    "дистрибьюторов, оптовых поставщиков — даже если на странице нет точного SKU "
                    "из запроса (достаточно, что они в этой сфере). "
                    "УЗОСТЬ: одна сфера. Жёстко отсекай смежные, но другие рынки из sphere_exclude "
                    "(пример: удобрения для растений ≠ кормовые/для скота/ветеринария). "
                    "Если по evidence нельзя понять сферу — confidence=low или не включай. "
                    "Исключай маркетплейсы бытовой техники и явный нерелевантный ритейл. "
                    "Верни JSON: {\"summary\":\"...\",\"findings\":[{"
                    "\"company_name\":\"\",\"website\":\"\",\"email\":\"\",\"phone\":\"\","
                    "\"contact_person\":\"\",\"price_info\":\"\",\"product_match\":\"\","
                    "\"role\":\"manufacturer|distributor|wholesaler|retailer|unknown\","
                    "\"source_url\":\"\",\"notes\":\"\",\"confidence\":\"high|medium|low\""
                    "}]}. Максимум 16 findings. "
                    "product_match — что именно из ассортимента/сферы совпало. "
                    "notes — кратко почему компания в этой сфере. website — главный сайт."
                ),
                user=(
                    f"Запрос менеджера:\n{q}\n{product_hint}\nФокус: {focus}\n"
                    f"{sphere_block}\n"
                    f"Поисковые запросы: {queries}\n\n"
                    f"Evidence ({len(evidence)} источников):\n"
                    f"{json.dumps(evidence, ensure_ascii=False)[:28000]}"
                ),
                temperature=0.15,
                timeout=120.0,
            )
            raw_findings = extracted.get("findings") if isinstance(extracted, dict) else None
            if isinstance(raw_findings, list):
                findings = raw_findings
            if isinstance(extracted, dict):
                summary = str(extracted.get("summary") or "")
        else:
            summary = (
                "Веб-поиск не вернул ссылок (возможно блокировка поисковиков или нет VPN). "
                "Проверьте интернет/прокси и повторите."
            )

        role_labels = {
            "manufacturer": "производитель",
            "distributor": "дистрибьютор",
            "wholesaler": "опт",
            "retailer": "розница",
            "unknown": "",
        }
        saved = 0
        already = 0
        for f in findings:
            if not isinstance(f, dict):
                continue
            name = str(f.get("company_name") or "").strip()
            if not name:
                continue
            existing = find_existing_supplier(
                conn,
                company_name=name,
                email=str(f.get("email") or ""),
                website=str(f.get("website") or ""),
            )
            added_id = existing["id"] if existing else None
            if existing:
                already += 1
            notes = str(f.get("notes") or "").strip()
            role = str(f.get("role") or "").strip().lower()
            role_ru = role_labels.get(role, "")
            if role_ru:
                notes = f"Роль: {role_ru}. {notes}".strip()
            conn.execute(
                """
                INSERT INTO search_findings
                  (session_id, company_name, website, email, phone, contact_person,
                   price_info, product_match, source_url, notes, confidence,
                   added_supplier_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    name,
                    str(f.get("website") or "").strip(),
                    str(f.get("email") or "").strip(),
                    str(f.get("phone") or "").strip(),
                    str(f.get("contact_person") or "").strip(),
                    str(f.get("price_info") or "").strip(),
                    str(f.get("product_match") or "").strip(),
                    str(f.get("source_url") or "").strip(),
                    notes,
                    str(f.get("confidence") or "medium").strip(),
                    added_id,
                    _now(),
                ),
            )
            saved += 1

        meta = {
            "queries": queries,
            "focus": focus,
            "sphere": sphere,
            "sphere_include": sphere_include,
            "sphere_exclude": sphere_exclude,
            "hits": [{"url": h["url"], "title": h.get("title"), "engine": h.get("engine")} for h in hits],
            "pages_fetched": sum(1 for p in pages if p.get("text")),
        }
        if not summary:
            summary = (
                f"Найдено карточек: {saved}"
                + (f", уже в базе: {already}" if already else "")
                + f". Поисковых ссылок: {len(hits)}."
            )
        else:
            summary = f"{summary} (ссылок: {len(hits)}, карточек: {saved}" + (
                f", уже в базе: {already})" if already else ")"
            )
        if sphere:
            summary = f"Сфера: {sphere}. {summary}"

        conn.execute(
            "UPDATE search_sessions SET status=?, summary=?, raw_hits=? WHERE id=?",
            ("done", summary, json.dumps(meta, ensure_ascii=False), session_id),
        )
        conn.commit()
        return {"session_id": session_id, "summary": summary, "findings": saved, "hits": len(hits)}
    except Exception as exc:
        conn.execute(
            "UPDATE search_sessions SET status=?, summary=? WHERE id=?",
            ("error", str(exc)[:500], session_id),
        )
        conn.commit()
        raise


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


def _norm_name(name: str) -> str:
    s = (name or "").lower().replace("ё", "е")
    s = re.sub(r"[\"«»“”'`]+", "", s)
    s = re.sub(r"\b(ооо|оао|зао|пао|ип|ltd|llc|inc|gmbh)\b", " ", s)
    s = re.sub(r"[^a-zа-я0-9]+", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip()


def _norm_domain(website: str) -> str:
    raw = (website or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        host = (urlparse(raw).netloc or "").lower()
    except Exception:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def find_existing_supplier(
    conn,
    *,
    company_name: str = "",
    email: str = "",
    website: str = "",
) -> dict[str, Any] | None:
    """Match by email, website domain, or company name."""
    email_n = _norm_email(email)
    domain = _norm_domain(website)
    name_n = _norm_name(company_name)

    if email_n and not email_n.endswith("@example.invalid"):
        row = conn.execute(
            "SELECT * FROM suppliers WHERE lower(trim(email))=?",
            (email_n,),
        ).fetchone()
        if row:
            return dict(row)

    suppliers = rows_to_dicts(conn.execute("SELECT * FROM suppliers").fetchall())
    if domain:
        for s in suppliers:
            if _norm_domain(s.get("website") or "") == domain:
                return s
            # email domain often equals site
            em = _norm_email(s.get("email") or "")
            if "@" in em and em.split("@", 1)[1] == domain:
                return s

    if name_n and len(name_n) >= 4:
        for s in suppliers:
            sn = _norm_name(s.get("name") or "")
            if not sn:
                continue
            if sn == name_n or name_n in sn or sn in name_n:
                return s
    return None


def annotate_findings_with_db(conn, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark findings that already exist in suppliers (without inserting)."""
    for f in findings:
        existing = None
        if f.get("added_supplier_id"):
            row = conn.execute(
                "SELECT * FROM suppliers WHERE id=?",
                (f["added_supplier_id"],),
            ).fetchone()
            if row:
                existing = dict(row)
        if not existing:
            existing = find_existing_supplier(
                conn,
                company_name=f.get("company_name") or "",
                email=f.get("email") or "",
                website=f.get("website") or "",
            )
            if existing and not f.get("added_supplier_id"):
                conn.execute(
                    "UPDATE search_findings SET added_supplier_id=? WHERE id=?",
                    (existing["id"], f["id"]),
                )
                f["added_supplier_id"] = existing["id"]

        f["already_in_db"] = bool(existing)
        f["matched_supplier"] = existing
    return findings


def get_session(conn, session_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM search_sessions WHERE id=?", (session_id,)).fetchone()
    if not row:
        return None
    data = dict(row)
    findings = rows_to_dicts(
        conn.execute(
            "SELECT * FROM search_findings WHERE session_id=? ORDER BY id",
            (session_id,),
        ).fetchall()
    )
    data["findings"] = annotate_findings_with_db(conn, findings)
    meta: dict[str, Any] = {}
    raw = (data.get("raw_hits") or "").strip()
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                meta = parsed
        except json.JSONDecodeError:
            meta = {"hits_count": raw}
    else:
        meta = {"hits_count": raw}
    data["search_meta"] = meta
    conn.commit()
    return data


def list_sessions(conn, limit: int = 20) -> list[dict]:
    return rows_to_dicts(
        conn.execute(
            "SELECT * FROM search_sessions ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    )


def add_finding_as_supplier(
    conn,
    *,
    finding_id: int,
    product_ids: list[int],
) -> dict[str, Any]:
    f = conn.execute("SELECT * FROM search_findings WHERE id=?", (finding_id,)).fetchone()
    if not f:
        raise ValueError("Карточка не найдена")

    product_ids = [int(x) for x in product_ids if x]
    sess = conn.execute(
        "SELECT product_id FROM search_sessions WHERE id=?",
        (f["session_id"],),
    ).fetchone()
    if sess and sess["product_id"]:
        spid = int(sess["product_id"])
        if spid not in product_ids:
            product_ids.append(spid)

    # Prefer already-linked supplier from this finding
    existing = None
    if f["added_supplier_id"]:
        row = conn.execute(
            "SELECT * FROM suppliers WHERE id=?",
            (int(f["added_supplier_id"]),),
        ).fetchone()
        if row:
            existing = dict(row)
    if not existing:
        existing = find_existing_supplier(
            conn,
            company_name=f["company_name"] or "",
            email=f["email"] or "",
            website=f["website"] or "",
        )

    if existing:
        sid = int(existing["id"])
        for pid in product_ids:
            conn.execute(
                "INSERT OR IGNORE INTO product_suppliers (product_id, supplier_id) VALUES (?, ?)",
                (pid, sid),
            )
        conn.execute(
            "UPDATE search_findings SET added_supplier_id=? WHERE id=?",
            (sid, finding_id),
        )
        return {
            "supplier_id": sid,
            "created": False,
            "name": existing.get("name") or f["company_name"],
            "linked_products": len(product_ids),
        }

    email = (f["email"] or "").strip()
    if not email:
        host = "example.invalid"
        if f["website"]:
            try:
                host = urlparse(
                    f["website"] if "://" in f["website"] else "https://" + f["website"]
                ).netloc or host
            except Exception:
                pass
        email = f"info@{host}"

    notes_parts = []
    if f["website"]:
        notes_parts.append(f"Сайт: {f['website']}")
    if f["price_info"]:
        notes_parts.append(f"Цена с сайта: {f['price_info']}")
    if f["product_match"]:
        notes_parts.append(f"Позиция: {f['product_match']}")
    if f["source_url"]:
        notes_parts.append(f"Источник: {f['source_url']}")
    if f["notes"]:
        notes_parts.append(f["notes"])
    notes = "\n".join(notes_parts)

    cur = conn.execute(
        """
        INSERT INTO suppliers (name, email, contact_person, phone, website, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f["company_name"],
            email,
            f["contact_person"] or "",
            f["phone"] or "",
            f["website"] or "",
            notes,
            _now(),
        ),
    )
    sid = int(cur.lastrowid)

    for pid in product_ids:
        conn.execute(
            "INSERT OR IGNORE INTO product_suppliers (product_id, supplier_id) VALUES (?, ?)",
            (pid, sid),
        )

    conn.execute(
        "UPDATE search_findings SET added_supplier_id=? WHERE id=?",
        (sid, finding_id),
    )
    return {
        "supplier_id": sid,
        "created": True,
        "name": f["company_name"],
        "linked_products": len(product_ids),
    }

def ai_draft_rfq_letter(
    *,
    supplier_name: str,
    contact: str,
    items: list[dict[str, Any]],
    sender_name: str,
    company: str,
    token_mark: str,
    extra_instruction: str = "",
    letter_conditions: str = "",
    base_subject: str = "",
    base_body: str = "",
    position: str = "",
    phone: str = "",
    website: str = "",
    signature_note: str = "",
) -> dict[str, str]:
    """Polish template RFQ; weave in manager conditions; do not invent extra terms."""
    items_txt = "\n".join(
        f"- {it.get('name')} — {it.get('quantity')} {it.get('unit')}"
        + (f" ({it.get('note')})" if it.get("note") else "")
        for it in items
    )
    conditions = (letter_conditions or "").strip()
    data = chat_json(
        system=(
            "Ты помощник ЗАКУПЩИКА. Письмо — запрос коммерческого предложения К ПОСТАВЩИКУ. "
            "Мы покупаем, а не продаём. "
            "Опирайся на ЧЕРНОВИК по шаблону: сохрани структуру "
            "(приветствие, список позиций, условия запроса, просьба о цене/сроке/наличии, подпись, код). "
            "Условия и требования менеджера — это ФАКТЫ запроса: обязательно включи их в письмо "
            "(можно слегка отредактировать формулировку, но не выкидывай и не меняй смысл). "
            "Запрещено выдумывать условия, которых нет во входных данных "
            "(адреса, оплата, даты, обязательства). "
            "Пожелания к тону — только стиль, не новые факты. "
            "В подписи сохрани сайт и строку-комментарий, если они есть в черновике или у закупщика. "
            f"В теме ОБЯЗАТЕЛЬНО оставь код {token_mark}. "
            "Верни JSON {\"subject\":\"...\",\"body\":\"...\"}."
        ),
        user=(
            f"Закупщик: {sender_name}"
            + (f", {position}" if position else "")
            + (f", {company}" if company else "")
            + (f", тел. {phone}" if phone else "")
            + (f", сайт: {website}" if website else "")
            + (f"\nКомментарий в подписи: {signature_note}" if signature_note else "")
            + f"\nПоставщик (получатель): {supplier_name}"
            + (f"\nКонтакт: {contact}" if contact else "")
            + f"\nПозиции к закупке:\n{items_txt}\n"
            + (
                f"\n=== Условия и требования менеджера (обязательно в письме) ===\n{conditions}\n"
                if conditions
                else ""
            )
            + f"\n--- Черновик (шаблон), тема ---\n{base_subject}\n"
            + f"\n--- Черновик (шаблон), текст ---\n{base_body}\n"
            + (
                f"\nПожелания к тону (не факты):\n{extra_instruction}"
                if extra_instruction
                else ""
            )
        ),
        temperature=0.35,
    )
    if not isinstance(data, dict):
        raise AiError("Некорректный ответ AI")
    subject = str(data.get("subject") or "").strip() or (base_subject or "").strip()
    body = str(data.get("body") or "").strip() or (base_body or "").strip()
    if token_mark not in subject:
        subject = f"{subject} {token_mark}".strip()
    if not body:
        raise AiError("Пустое тело письма от AI")
    # Safety net: if AI dropped conditions, append a block
    if conditions:
        # rough check: at least half of non-empty lines appear somehow
        key_lines = [ln.strip() for ln in conditions.splitlines() if len(ln.strip()) >= 8]
        missing = [ln for ln in key_lines if ln.lower() not in body.lower()]
        if key_lines and len(missing) >= max(1, (len(key_lines) + 1) // 2):
            marker = "Дополнительные условия и требования по этому запросу:"
            if marker not in body:
                insert_at = body.find("Просьба указать")
                block = f"{marker}\n{conditions}\n\n"
                if insert_at >= 0:
                    body = body[:insert_at] + block + body[insert_at:]
                else:
                    body = body.rstrip() + "\n\n" + block
    return {"subject": subject, "body": body}


def ai_draft_followup(
    *,
    supplier_name: str,
    contact: str,
    notes: str,
    sender_name: str,
    company: str,
    token_mark: str,
    letter_conditions: str = "",
    prior_subject: str = "",
    position: str = "",
    phone: str = "",
    website: str = "",
    signature_note: str = "",
) -> dict[str, str]:
    """Turn manager notes into a polite clarification email to the supplier."""
    data = chat_json(
        system=(
            "Ты помощник ЗАКУПЩИКА. Нужно короткое письмо-уточнение К ПОСТАВЩИКУ. "
            "Сохрани все факты из заметок менеджера, не выдумывай новых условий. "
            "Тон: деловой, вежливый, на «вы». "
            "В конце добавь короткую подпись (имя, должность, компания, телефон, сайт, комментарий — если даны). "
            f"В теме обязательно оставь код {token_mark}. "
            "Верни JSON {\"subject\":\"...\",\"body\":\"...\"}."
        ),
        user=(
            f"Закупщик: {sender_name}"
            + (f", {position}" if position else "")
            + (f", {company}" if company else "")
            + (f", тел. {phone}" if phone else "")
            + (f", сайт: {website}" if website else "")
            + (f"\nКомментарий в подписи: {signature_note}" if signature_note else "")
            + f"\nПоставщик: {supplier_name}"
            + (f"\nКонтакт: {contact}" if contact else "")
            + (f"\nТема исходного запроса: {prior_subject}" if prior_subject else "")
            + (
                f"\nАктуальные условия запроса:\n{letter_conditions}"
                if letter_conditions
                else ""
            )
            + f"\n\nЗаметки менеджера для уточнения:\n{notes}"
        ),
        temperature=0.35,
    )
    if not isinstance(data, dict):
        raise AiError("Некорректный ответ AI")
    subject = str(data.get("subject") or "").strip() or f"Уточнение {token_mark}"
    body = str(data.get("body") or "").strip()
    if token_mark not in subject:
        subject = f"{subject} {token_mark}".strip()
    if not body:
        raise AiError("Пустое тело письма от AI")
    return {"subject": subject, "body": body}
