"""Shared Gemini (OpenAI-compatible) chat helper — free tier via AI Studio key."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger("ai_client")

# Для новых ключей Google 2.5-flash часто отдаёт 404 «no longer available to new users».
# Стабильные алиасы + актуальные flash.
DEFAULT_MODELS = (
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-2.0-flash",
)


class AiError(RuntimeError):
    pass


def chat_json(
    *,
    system: str,
    user: str,
    temperature: float = 0.2,
    timeout: float = 90.0,
) -> Any:
    """Call model and parse JSON from the reply (object or array)."""
    text = chat_text(system=system, user=user, temperature=temperature, timeout=timeout)
    return extract_json(text)


def _model_chain(settings) -> list[str]:
    ordered: list[str] = []
    for name in (settings.ai_model, settings.ai_fallback_model, *DEFAULT_MODELS):
        n = (name or "").strip()
        # 1.5 и «голый» 2.5-flash на новых ключах часто 404
        if not n or n in ordered:
            continue
        if n in {"gemini-1.5-flash", "gemini-1.5-pro", "models/gemini-1.5-flash"}:
            continue
        ordered.append(n)
    return ordered or list(DEFAULT_MODELS)


def _normalize_proxy(url: str) -> str:
    """httpx понимает socks5://, а Windows часто отдаёт socks4:// на тот же порт VPN."""
    u = (url or "").strip()
    if u.startswith("socks4://"):
        return "socks5://" + u[len("socks4://") :]
    if u.startswith("socks4a://"):
        return "socks5h://" + u[len("socks4a://") :]
    return u


def _detect_proxy() -> str | None:
    for key in (
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
    ):
        val = os.environ.get(key)
        if val:
            return _normalize_proxy(val)
    try:
        import urllib.request

        proxies = urllib.request.getproxies() or {}
        raw = proxies.get("https") or proxies.get("http") or proxies.get("socks")
        if raw:
            return _normalize_proxy(raw)
    except Exception:
        pass
    # типичный локальный VPN (Clash/V2Ray)
    for candidate in ("socks5://127.0.0.1:10808", "socks5h://127.0.0.1:10808"):
        return candidate
    return None


def _make_client(timeout: float) -> httpx.Client:
    """Из РФ Google AI обычно нужен VPN/proxy."""
    proxy = _detect_proxy()
    if proxy:
        try:
            return httpx.Client(proxy=proxy, timeout=timeout, trust_env=False)
        except Exception as exc:
            log.warning("Proxy %s unavailable (%s), trying direct", proxy, exc)
    return httpx.Client(timeout=timeout, trust_env=False)


def chat_text(
    *,
    system: str,
    user: str,
    temperature: float = 0.4,
    timeout: float = 90.0,
) -> str:
    get_settings.cache_clear()
    settings = get_settings()
    if not settings.ai_ready:
        raise AiError(
            "AI не настроен. Получите бесплатный ключ на https://aistudio.google.com "
            "и пропишите AI_API_KEY в .env"
        )

    url = settings.ai_base_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {settings.ai_api_key}"}
    models = _model_chain(settings)
    last_detail = ""

    try:
        with _make_client(timeout) as client:
            for model in models:
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": temperature,
                }
                r = client.post(url, headers=headers, json=payload)
                if r.status_code == 200:
                    if model != settings.ai_model:
                        log.info("AI ok via fallback model %s", model)
                    return (r.json()["choices"][0]["message"]["content"] or "").strip()

                last_detail = r.text[:500]
                low = last_detail.lower()
                if r.status_code in (404, 400) and (
                    "not found" in low
                    or "no longer available" in low
                    or "is not found" in low
                ):
                    log.warning("Model %s rejected (%s), trying next", model, r.status_code)
                    continue
                if r.status_code == 429:
                    log.warning("Model %s rate-limited, trying next", model)
                    continue
                if "location is not supported" in low:
                    raise AiError(
                        "Google AI недоступен из вашего региона. Включите VPN "
                        "(прокси на 127.0.0.1:10808) и повторите."
                    )
                raise AiError(f"AI HTTP {r.status_code}: {last_detail}")

            raise AiError(
                "Ни одна Gemini-модель не ответила. "
                f"Пробовали: {', '.join(models)}. Последний ответ: {last_detail}"
            )
    except AiError:
        raise
    except Exception as exc:
        raise AiError(str(exc)) from exc


def extract_json(text: str) -> Any:
    content = (text or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", content)
        if not m:
            raise AiError(f"Модель вернула не JSON: {content[:240]}")
        return json.loads(m.group(1))
