"""Загрузка веб-страницы и извлечение текста статьи."""
from __future__ import annotations

import asyncio
import logging

import httpx
import trafilatura

logger = logging.getLogger(__name__)

_MAX_BYTES = 2 * 1024 * 1024
_MIN_TEXT = 200
_HEADERS = {
    # Честный UA с контактом: Википедия и др. отдают 403 «браузерному» UA без браузерного TLS
    "User-Agent": "BrainBot/1.0 (https://github.com/njhgfd203/brain; personal reading assistant)",
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru,en;q=0.8",
}


def _extract(html: str) -> tuple[str | None, str | None]:
    text = trafilatura.extract(html, include_comments=False, include_tables=False)
    meta = trafilatura.extract_metadata(html)
    title = getattr(meta, "title", None) if meta else None
    return title, text


async def fetch_article(url: str) -> tuple[str, str] | None:
    """Скачивает страницу и возвращает (title, text) или None, если не удалось/не статья."""
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=15, headers=_HEADERS
        ) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                ctype = resp.headers.get("content-type", "").lower()
                if "html" not in ctype and not ctype.startswith("text/"):
                    return None
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) >= _MAX_BYTES:
                        break
                encoding = resp.encoding or "utf-8"
        html = bytes(buf[:_MAX_BYTES]).decode(encoding, errors="replace")

        title, text = await asyncio.to_thread(_extract, html)
        if not text or len(text) < _MIN_TEXT:
            return None
        return (title or "").strip(), text.strip()
    except Exception as e:
        logger.warning("fetch_article failed for %s: %s", url, e)
        return None
