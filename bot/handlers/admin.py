"""Административные команды: /reindex, /stats."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from rag import indexer

logger = logging.getLogger(__name__)

router = Router()


@router.message(Command("morning"))
async def cmd_morning(message: Message) -> None:
    """Ручной запуск утреннего дайджеста (для проверки)."""
    from bot.scheduler import send_morning_digest
    await send_morning_digest(message.bot)


@router.message(Command("evening"))
async def cmd_evening(message: Message) -> None:
    """Ручной запуск вечерней сводки (для проверки)."""
    from bot.scheduler import send_evening_review
    await send_evening_review(message.bot)


@router.message(Command("weekly"))
async def cmd_weekly(message: Message) -> None:
    """Ручной запуск итогов недели (для проверки)."""
    from bot.scheduler import send_weekly_review
    await send_weekly_review(message.bot)


@router.message(Command("reindex"))
async def cmd_reindex(message: Message) -> None:
    """Переиндексирует всю папку knowledge/."""
    await message.answer("Индексирую базу знаний...")
    try:
        summary = await asyncio.to_thread(indexer.reindex_all)
        text = (
            f"Реиндексация завершена.\n\n"
            f"Файлов обработано: {summary['files']}\n"
            f"Проиндексировано заново: {summary['indexed']}\n"
            f"Пропущено (без изменений): {summary['skipped']}\n"
            f"Пустых файлов: {summary['empty']}\n"
            f"Удалено из индекса: {summary['removed']}\n"
            f"Итого чанков в базе: {summary['chunks']}"
        )
        await message.answer(text)
    except Exception:
        logger.exception("Reindex failed")
        await message.answer("Ошибка при реиндексации. Подробности в логах.")
