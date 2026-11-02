"""Пересланные сообщения и ссылки: короткая выжимка + сохранение в inbox/ + индексация."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path

from aiogram import F, Router
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
)

from bot import keyboards as kb
from bot.config import settings
from bot.handlers import files
from bot.handlers.notes import save_note
from bot.tools.llm import summarize_short
from bot.tools.paths import bot_output_dir
from bot.tools.web import fetch_article
from rag import indexer

logger = logging.getLogger(__name__)

router = Router()

_URL_RE = re.compile(r"https?://\S+")
_MIN_FREE_TEXT = 80  # если текста кроме ссылки меньше — считаем сообщение «просто ссылкой»
_MENU_BUTTONS = (
    kb.BTN_TODAY, kb.BTN_WEEK, kb.BTN_HABITS, kb.BTN_TASK, kb.BTN_MEET, kb.BTN_NOTE,
)

_extract_kb = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="📋 Извлечь задачи", callback_data="extractfile")]]
)


def _q(value: str) -> str:
    """Безопасная YAML-строка: JSON-строка в двойных кавычках валидна как YAML."""
    return json.dumps(" ".join(value.split()), ensure_ascii=False)


def _find_urls(message: Message) -> list[str]:
    """URL из entities (url / text_link) и regex-ом по тексту."""
    text = message.text or ""
    urls: list[str] = []
    for ent in message.entities or []:
        if ent.type == "url":
            urls.append(ent.extract_from(text))
        elif ent.type == "text_link" and ent.url:
            urls.append(ent.url)
    urls.extend(_URL_RE.findall(text))
    result: list[str] = []
    for u in urls:
        u = u.rstrip(".,;:!?)»\"'")
        if u and u not in result:
            result.append(u)
    return result


def _text_without_urls(text: str) -> str:
    return _URL_RE.sub("", text).strip()


def _is_link_message(message: Message) -> bool:
    """Фильтр: сообщение с URL и коротким остальным текстом (не команда и не кнопка меню)."""
    text = message.text
    if not text or text.startswith("/") or text in _MENU_BUTTONS:
        return False
    if not _find_urls(message):
        return False
    return len(_text_without_urls(text)) < _MIN_FREE_TEXT


def _split_summary(summary: str) -> tuple[str, str]:
    """Делит выжимку на (заголовок из первой «# …» строки, остальное тело)."""
    lines = (summary or "").strip().splitlines()
    title = ""
    if lines and lines[0].lstrip().startswith("#"):
        title = lines[0].lstrip("# ").strip()
        lines = lines[1:]
    return title, "\n".join(lines).strip()


def _origin_name(message: Message) -> str:
    origin = message.forward_origin
    if isinstance(origin, MessageOriginUser):
        return origin.sender_user.full_name
    if isinstance(origin, MessageOriginHiddenUser):
        return origin.sender_user_name
    if isinstance(origin, MessageOriginChat):
        chat = origin.sender_chat
        title = chat.title or "чат"
        return f"{title} (@{chat.username})" if chat.username else title
    if isinstance(origin, MessageOriginChannel):
        chat = origin.chat
        title = chat.title or "канал"
        return f"{title} (@{chat.username})" if chat.username else title
    return "неизвестно"


def _unique_path(prefix: str) -> Path:
    inbox_dir = bot_output_dir("inbox")
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    dest = inbox_dir / f"{prefix}-{stamp}.md"
    i = 2
    while dest.exists():
        dest = inbox_dir / f"{prefix}-{stamp}-{i}.md"
        i += 1
    return dest


def _frontmatter(title: str, tag: str, extra: dict[str, str]) -> str:
    # domain: inbox + папка <bot_inbox_dir>/inbox/ — как в notes.save_note
    lines = [
        "---",
        f"title: {_q(title)}",
        f"date: {datetime.now().strftime('%Y-%m-%d')}",
        "domain: inbox",
        f"tags: [{tag}]",
        "type: note",
    ]
    lines += [f"{k}: {_q(v)}" for k, v in extra.items()]
    lines.append("---")
    return "\n".join(lines) + "\n\n"


async def _save_and_reply(
    message: Message, dest: Path, content: str, reply: str
) -> None:
    try:
        dest.write_text(content, encoding="utf-8")
        await asyncio.to_thread(indexer.index_file, dest)
    except Exception:
        logger.exception("Inbox save/index failed for %s", dest)
        await message.answer("Не удалось сохранить. Попробуй ещё раз.")
        return
    if message.from_user:
        files._last_upload[message.from_user.id] = str(dest)
    await message.answer(reply[:4000], reply_markup=_extract_kb)


async def _process_link(
    message: Message, url: str, comment: str, source: str | None = None
) -> None:
    await message.answer("🔗 Читаю страницу…")
    await message.bot.send_chat_action(message.chat.id, "typing")

    article = await fetch_article(url)
    if article is None:
        await save_note(message.text or message.caption or url)
        await message.answer(
            "Не смог прочитать страницу (закрыта/не статья) — сохранил ссылку заметкой."
        )
        return

    page_title, text = article
    try:
        summary = await summarize_short(f"{page_title}\n\n{text}")
    except Exception:
        logger.exception("summarize_short failed for link %s", url)
        summary = ""
    sum_title, sum_body = _split_summary(summary)
    title = page_title or sum_title or url[:60]

    extra = {"url": url}
    if source:
        extra["source"] = source
    body = f"## Выжимка\n{sum_body}\n\n"
    if comment:
        body += f"## Мой комментарий\n{comment}\n\n"
    body += f"## Текст страницы\n{text[:30000]}\n"

    dest = _unique_path("link")
    reply = f"🔗 {title}\n\n{sum_body}\n\n{url}".replace("\n\n\n", "\n\n")
    await _save_and_reply(message, dest, _frontmatter(title, "link", extra) + body, reply)


@router.message(F.forward_origin, (F.text | F.caption))
async def on_forward(message: Message) -> None:
    text = (message.text or message.caption or "").strip()
    if not text or text.startswith("/"):
        return
    source = _origin_name(message)

    urls = _find_urls(message) if message.text else _URL_RE.findall(text)
    rest = _text_without_urls(text)
    if urls and len(rest) < _MIN_FREE_TEXT:
        await _process_link(message, urls[0].rstrip(".,;:!?)»\"'"), rest, source)
        return

    await message.bot.send_chat_action(message.chat.id, "typing")
    try:
        summary = await summarize_short(text)
    except Exception:
        logger.exception("summarize_short failed for forward")
        summary = ""
    sum_title, sum_body = _split_summary(summary)
    title = sum_title or " ".join(text.split())[:60]

    body = f"## Выжимка\n{sum_body}\n\n## Оригинал\n{text}\n"
    dest = _unique_path("fwd")
    reply = f"📨 Сохранил: {title}\n\n{sum_body}".strip()
    await _save_and_reply(
        message, dest, _frontmatter(title, "forward", {"source": source}) + body, reply
    )


@router.message(F.text, _is_link_message)
async def on_link(message: Message) -> None:
    url = _find_urls(message)[0]
    comment = _text_without_urls(message.text)
    await _process_link(message, url, comment)
