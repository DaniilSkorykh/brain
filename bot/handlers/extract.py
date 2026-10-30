"""Извлечение задач из текста заметки/встречи через LLM + выбор кандидатов кнопками.

Регистрируется ДО ask.router. Кандидаты хранятся в FSM (ExtractFlow.review).
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.handlers.tasks import _format_due
from bot.tools.llm import extract_tasks
from db.database import add_task

logger = logging.getLogger(__name__)

router = Router()


class ExtractFlow(StatesGroup):
    review = State()


def _build_markup(candidates: list[dict], added: list[int]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for i, c in enumerate(candidates):
        done = i in added
        label = ("✅ " if done else "➕ ") + c["text"][:30]
        rows.append([InlineKeyboardButton(text=label, callback_data=f"addone:{i}")])
    if len(candidates) > 1:
        rows.append([InlineKeyboardButton(text="➕ Добавить все", callback_data="addall")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_list(candidates: list[dict]) -> str:
    lines = [f"📋 Нашёл задач: {len(candidates)}. Отметь, что добавить:", ""]
    for i, c in enumerate(candidates, 1):
        due = f" — {_format_due(c['due_date'], c.get('due_time'))}" if c.get("due_date") else ""
        lines.append(f"{i}. {c['text']} ({c['domain']}){due}")
    return "\n".join(lines)


async def present_candidates(
    message: Message,
    candidates: list[dict],
    state: FSMContext,
    empty_text: str = "Задач в тексте не нашёл 🤷",
) -> None:
    """Показывает извлечённые задачи с кнопками. Вызывается из /extract, files.py и voice.py."""
    if not candidates:
        await message.answer(empty_text)
        return
    await state.set_state(ExtractFlow.review)
    await state.update_data(candidates=candidates, added=[])
    await message.answer(_format_list(candidates), reply_markup=_build_markup(candidates, []))


@router.message(Command("extract"))
async def cmd_extract(message: Message, command: CommandObject, state: FSMContext) -> None:
    # Источник текста: reply на сообщение, либо аргумент команды
    source = ""
    if message.reply_to_message and message.reply_to_message.text:
        source = message.reply_to_message.text
    elif command.args:
        source = command.args
    if not source.strip():
        await message.answer(
            "Извлечь задачи: ответь командой /extract на сообщение с текстом встречи,\n"
            "или напиши /extract <текст>."
        )
        return
    await message.answer("🔍 Ищу задачи…")
    candidates = await extract_tasks(source)
    await present_candidates(message, candidates, state)


@router.callback_query(StateFilter(ExtractFlow.review), F.data.startswith("addone:"))
async def cb_add_one(callback: CallbackQuery, state: FSMContext) -> None:
    idx = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    candidates: list[dict] = data.get("candidates", [])
    added: list[int] = data.get("added", [])
    if idx >= len(candidates):
        await callback.answer("Не нашёл задачу")
        return
    if idx in added:
        await callback.answer("Уже добавлено")
        return
    c = candidates[idx]
    await add_task(c["text"], c["domain"], c["due_date"], c.get("due_time"))
    added.append(idx)
    await state.update_data(added=added)
    await callback.answer(f"✅ {c['text'][:40]}")
    if callback.message:
        await callback.message.edit_reply_markup(reply_markup=_build_markup(candidates, added))


@router.callback_query(StateFilter(ExtractFlow.review), F.data == "addall")
async def cb_add_all(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    candidates: list[dict] = data.get("candidates", [])
    added: list[int] = data.get("added", [])
    new = 0
    for i, c in enumerate(candidates):
        if i not in added:
            await add_task(c["text"], c["domain"], c["due_date"], c.get("due_time"))
            new += 1
    await state.clear()
    await callback.answer(f"Добавил: {new}")
    if callback.message:
        await callback.message.edit_text(f"✅ Добавил задач: {new}. Смотри /today и /week.")
