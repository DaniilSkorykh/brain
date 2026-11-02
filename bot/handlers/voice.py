"""Голосовые/аудио: транскрипция (Whisper) → классификация → роутинг.

Короткие сообщения — авто-классификация (заметка/задача/вопрос/встреча).
Длинные записи (>= long_audio_min_sec) — режим лекции: транскрипт + конспект,
сохранение в базу знаний, кнопка извлечения задач.

Регистрируется ДО ask.router.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.config import settings
from bot.handlers import extract as extract_mod
from bot.handlers.ask import _handle_question
from bot.handlers.cards import send_task_card
from bot.handlers.meetings import add_meeting_and_reply, meeting_from_parsed
from bot.handlers.notes import save_note
from bot.handlers.tasks import create_task_from_parsed
from bot.tools.llm import extract_tasks, parse_message, summarize_transcript
from bot.tools.paths import bot_output_dir
from bot.tools.tgfiles import fetch_to
from bot.tools.transcribe import temp_path, transcribe, transcribe_long
from rag import indexer

logger = logging.getLogger(__name__)

router = Router()

_DOMAINS = ("technored", "ministry", "personal", "inbox")

_lecture_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="technored", callback_data="lecdom:technored"),
            InlineKeyboardButton(text="ministry", callback_data="lecdom:ministry"),
        ],
        [
            InlineKeyboardButton(text="personal", callback_data="lecdom:personal"),
            InlineKeyboardButton(text="inbox", callback_data="lecdom:inbox"),
        ],
    ]
)


class LectureFlow(StatesGroup):
    choosing_domain = State()


async def _download_voice(message: Message) -> str:
    """Скачивает голосовое/аудио во временный файл, возвращает путь."""
    media = message.voice or message.audio
    dst = temp_path(".oga")
    await fetch_to(message.bot, media.file_id, dst)
    return dst


def _duration(message: Message) -> int:
    media = message.voice or message.audio
    return getattr(media, "duration", 0) or 0


@router.message(F.voice | F.audio)
async def handle_voice(message: Message, state: FSMContext) -> None:
    if _duration(message) >= settings.long_audio_min_sec:
        await _handle_lecture(message, state)
        return

    await message.bot.send_chat_action(message.chat.id, "typing")
    src = None
    try:
        src = await _download_voice(message)
        text = await transcribe(src)
    except Exception:
        logger.exception("Voice transcription failed")
        await message.answer("Не получилось распознать голосовое. Попробуй ещё раз.")
        return
    finally:
        if src and os.path.exists(src):
            try:
                os.remove(src)
            except OSError:
                pass

    if not text:
        await message.answer("Не разобрал голосовое — тишина или слишком тихо. Попробуй ещё раз.")
        return

    prefix = f"🎤 Распознал: «{text}»"
    parsed = await parse_message(text)

    if parsed is None or parsed["kind"] == "note":
        # parse_message упал или это заметка — сохраняем исходный текст
        _, indexed = await save_note(text)
        reply = prefix + "\n\n📝 Сохранил как заметку"
        if not indexed:
            reply += " (индексация не удалась — /reindex)"
        await message.answer(reply)

    elif parsed["kind"] == "task":
        task = await create_task_from_parsed(parsed)
        await send_task_card(message, task, raw=text, prefix=prefix)

    elif parsed["kind"] == "meeting":
        meeting = meeting_from_parsed(parsed)
        if meeting is not None:
            await add_meeting_and_reply(message, text, meeting, prefix=prefix)
        else:
            await message.answer(prefix)
            await add_meeting_and_reply(message, text)  # нет даты — старый путь (dateparser)

    else:  # question
        await message.answer(prefix + "\n\n💬 Отвечаю…")
        await _handle_question(message, text)


async def _handle_lecture(message: Message, state: FSMContext) -> None:
    """Длинная запись → транскрипт (чанками) + конспект → выбор раздела → сохранение."""
    minutes = max(1, _duration(message) // 60)
    status = await message.answer(
        f"⏳ Распознаю запись (~{minutes} мин). Это займёт пару минут…"
    )

    async def progress(done: int, total: int) -> None:
        if total <= 1:
            return
        try:
            await status.edit_text(f"⏳ Распознаю запись… фрагмент {min(done + 1, total)}/{total}")
        except Exception:
            pass  # игнор частых правок/без изменений

    src = None
    try:
        src = await _download_voice(message)
    except Exception:
        logger.exception("Lecture download failed")
        await status.edit_text(
            "Не смог скачать запись. Если файл больше 20 МБ — Telegram не отдаёт его боту; "
            "пришли как голосовое сообщение или раздели на части."
        )
        return

    try:
        try:
            transcript = await transcribe_long(src, progress_cb=progress)
        except Exception:
            logger.exception("Lecture transcription failed")
            await status.edit_text("Не получилось распознать запись. Попробуй ещё раз.")
            return
    finally:
        if src and os.path.exists(src):
            try:
                os.remove(src)
            except OSError:
                pass

    if not transcript:
        await status.edit_text("Не разобрал запись — тишина или плохое качество звука.")
        return

    await status.edit_text("📝 Делаю конспект…")
    try:
        summary = await summarize_transcript(transcript)
    except Exception:
        logger.exception("Lecture summary failed")
        summary = ""

    if not summary:
        summary = "## Конспект\n(не удалось сжать — ниже полный транскрипт)"

    await state.set_state(LectureFlow.choosing_domain)
    await state.update_data(transcript=transcript, summary=summary)

    await status.delete()
    # Конспект может быть длинным — Telegram режет на 4096; шлём как есть (обычно влезает)
    await message.answer(summary[:4000])
    await message.answer("📂 В какой раздел сохранить конспект?", reply_markup=_lecture_kb)


def _derive_title(summary: str) -> str:
    """Берёт заголовок из конспекта: первая значимая строка без '#'."""
    for line in summary.splitlines():
        s = line.strip().lstrip("#").strip()
        if s and not s.lower().startswith("тема"):
            return s[:80]
    return "Конспект записи"


@router.callback_query(F.data.startswith("lecdom:"))
async def on_lecture_domain(callback: CallbackQuery, state: FSMContext) -> None:
    domain = callback.data.split(":", 1)[1]
    if domain not in _DOMAINS:
        await callback.answer("Неизвестный раздел")
        return

    data = await state.get_data()
    await state.clear()
    transcript = data.get("transcript")
    summary = data.get("summary", "")
    if not transcript:
        await callback.answer("Текст потерялся — пришли запись заново.")
        return

    await callback.answer("Сохраняю…")

    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d-%H%M")
    title = _derive_title(summary)

    dest_dir = bot_output_dir(domain)
    dest = dest_dir / f"transcript-{stamp}.md"

    frontmatter = (
        f"---\n"
        f"title: {title}\n"
        f"date: {now.strftime('%Y-%m-%d')}\n"
        f"domain: {domain}\n"
        f"tags: [transcript]\n"
        f"type: meeting\n"
        f"---\n\n"
    )
    body = f"{summary}\n\n## Полный транскрипт\n\n{transcript}\n"
    try:
        dest.write_text(frontmatter + body, encoding="utf-8")
        await asyncio.to_thread(indexer.index_file, dest)
    except Exception:
        logger.exception("Lecture save/index failed")
        if callback.message:
            await callback.message.edit_text("Не удалось сохранить конспект.")
        return

    if not callback.message:
        return
    await callback.message.edit_text(
        f"📄 Конспект сохранён: {domain}/{dest.name}. Ищется через /ask.\n"
        f"🔍 Ищу задачи…"
    )
    # summary первым — в нём раздел «Задачи»
    candidates = await extract_tasks(f"{summary}\n\n{transcript}")
    await extract_mod.present_candidates(
        callback.message, candidates, state, empty_text="Задач в записи не нашёл 🤷"
    )
