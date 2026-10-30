import asyncio
import logging
from datetime import datetime
from pathlib import Path

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from bot.config import settings
from rag import indexer

logger = logging.getLogger(__name__)

router = Router()

HELP_TEXT = (
    "Привет! Я личный ассистент Даниила.\n\n"
    "Доступные команды:\n"
    "/note <текст> — сохранить заметку в inbox\n"
    "/ask <вопрос> — спросить по базе знаний (или просто напиши текст)\n"
    "/reindex — переиндексировать базу знаний\n"
    "/task <текст> [дата] [#домен] — добавить задачу\n"
    "/today — задачи на сегодня и просроченные\n"
    "/week — обзор задач на 7 дней по доменам\n"
    "/habit <текст> [расписание] — повторяющаяся привычка (стрики 🔥)\n"
    "/habits — список привычек и стриков\n"
    "/meet <тема> <когда> — добавить встречу (напомню за час)\n"
    "/meetings — ближайшие встречи\n"
    "/help — показать эту справку\n\n"
    "📎 Можно прислать .md-файл — сохраню в базу знаний (спрошу раздел).\n"
    "   После загрузки предложу извлечь из него задачи.\n"
    "📨 Перешли сообщение — сделаю выжимку и сохраню в inbox.\n"
    "🔗 Пришли ссылку на статью — прочитаю, сделаю выжимку и сохраню.\n"
    "🎤 Голосовое — сам пойму: заметка, задача, вопрос или встреча.\n"
    "🎙 Длинная запись (от 5 мин) — сделаю транскрипт + конспект и сохраню.\n"
    "Утром в 9:00 пришлю задачи и привычки, вечером в 23:00 — итоги.\n\n"
    "Появятся в следующих этапах:\n"
    "/search, /stats"
)


@router.message(Command("start", "help"))
async def cmd_start(message: Message) -> None:
    from bot.keyboards import main_keyboard
    await message.answer(HELP_TEXT, reply_markup=main_keyboard)


async def save_note(text: str) -> tuple[Path, bool]:
    """Сохраняет заметку в inbox/YYYY-MM-DD.md и индексирует. Возвращает (файл, indexed_ok).

    Переиспользуется командой /note, FSM-флоу и голосовыми сообщениями.
    """
    today = datetime.now()
    date_str = today.strftime("%Y-%m-%d")
    time_str = today.strftime("%H:%M")

    inbox_dir = Path(settings.knowledge_dir) / "inbox"
    inbox_dir.mkdir(parents=True, exist_ok=True)

    note_file = inbox_dir / f"{date_str}.md"

    if not note_file.exists():
        frontmatter = (
            f"---\n"
            f"title: Inbox {date_str}\n"
            f"date: {date_str}\n"
            f"domain: inbox\n"
            f"tags: [inbox]\n"
            f"type: note\n"
            f"---\n"
        )
        note_file.write_text(frontmatter, encoding="utf-8")

    entry = f"\n## {time_str}\n\n{text.strip()}\n"
    with note_file.open("a", encoding="utf-8") as f:
        f.write(entry)

    # Индексируем файл сразу после сохранения (в том же процессе — без stale-индекса)
    try:
        await asyncio.to_thread(indexer.index_file, note_file)
        return note_file, True
    except Exception:
        logger.exception("Failed to index note file %s", note_file)
        return note_file, False


@router.message(Command("note"))
async def cmd_note(message: Message, command: CommandObject) -> None:
    text = command.args
    if not text or not text.strip():
        await message.answer(
            "Использование: /note <текст>\n"
            "Например: /note Обсудить с командой новый дизайн"
        )
        return

    note_file, indexed = await save_note(text.strip())
    await message.answer(f"Сохранено в inbox/{note_file.name}")
    if not indexed:
        await message.answer("(Заметка сохранена, но индексация не удалась — попробуй /reindex)")
