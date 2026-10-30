"""UI-роутер: кнопки главного меню + FSM-флоу добавления задачи/заметки/встречи.

Регистрируется ДО ask.router (catch-all). Кнопки матчатся точными фильтрами,
ввод в диалоге — по состоянию FSM.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from bot import keyboards as kb
from bot.handlers.cards import send_task_card
from bot.handlers.habits import cmd_habits
from bot.handlers.meetings import add_meeting_and_reply
from bot.handlers.notes import save_note
from bot.handlers.tasks import cmd_today, cmd_week, create_task_from_text

logger = logging.getLogger(__name__)

router = Router()


class AddFlow(StatesGroup):
    task = State()
    note = State()
    meeting = State()


# --- Кнопки без аргумента: переиспользуем готовые хендлеры ---

@router.message(F.text == kb.BTN_TODAY)
async def btn_today(message: Message, state: FSMContext) -> None:
    await state.clear()
    await cmd_today(message)


@router.message(F.text == kb.BTN_WEEK)
async def btn_week(message: Message, state: FSMContext) -> None:
    await state.clear()
    await cmd_week(message)


@router.message(F.text == kb.BTN_HABITS)
async def btn_habits(message: Message, state: FSMContext) -> None:
    await state.clear()
    await cmd_habits(message)


# --- Кнопки-флоу: спрашиваем ввод, ловим следующее сообщение ---

@router.message(F.text == kb.BTN_TASK)
async def btn_task(message: Message, state: FSMContext) -> None:
    await state.set_state(AddFlow.task)
    await message.answer("✏️ Что за задача? Можно с датой и #доменом.\n(или /cancel)")


@router.message(F.text == kb.BTN_NOTE)
async def btn_note(message: Message, state: FSMContext) -> None:
    await state.set_state(AddFlow.note)
    await message.answer("📝 Текст заметки?\n(или /cancel)")


@router.message(F.text == kb.BTN_MEET)
async def btn_meet(message: Message, state: FSMContext) -> None:
    await state.set_state(AddFlow.meeting)
    await message.answer("🤝 Когда и о чём встреча?\nНапример: «по REDWELD завтра в 15:00»\n(или /cancel)")


@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() is None:
        await message.answer("Нечего отменять.")
        return
    await state.clear()
    await message.answer("Отменил.")


# --- Приём ввода в состояниях ---

@router.message(StateFilter(AddFlow.task), F.text)
async def flow_task(message: Message, state: FSMContext) -> None:
    await state.clear()
    task = await create_task_from_text(message.text or "")
    if task is None:
        await message.answer("Не понял текст задачи. Попробуй ещё раз через ➕ Задача.")
        return
    await send_task_card(message, task, raw=(message.text or "").strip())


@router.message(StateFilter(AddFlow.note), F.text)
async def flow_note(message: Message, state: FSMContext) -> None:
    await state.clear()
    note_file, indexed = await save_note((message.text or "").strip())
    await message.answer(f"📝 Сохранено в inbox/{note_file.name}")
    if not indexed:
        await message.answer("(Заметка сохранена, но индексация не удалась — попробуй /reindex)")


@router.message(StateFilter(AddFlow.meeting), F.text)
async def flow_meeting(message: Message, state: FSMContext) -> None:
    await state.clear()
    await add_meeting_and_reply(message, (message.text or "").strip())
