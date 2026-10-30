"""Карточки задачи/встречи с inline-кнопками: смена типа, времени, удаление.

Регистрируется ДО menu.router и ask.router (FSM-состояние ловится раньше catch-all).
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot import keyboards as kb
from bot.handlers.meetings import _format_dt, meeting_from_parsed
from bot.handlers.notes import save_note
from bot.handlers.tasks import _format_due, create_task_from_parsed, human_recurrence
from bot.tools.llm import parse_message
from db.database import (
    add_meeting,
    clear_task_recurrence,
    delete_meeting,
    delete_task,
    get_meeting,
    get_task,
    update_meeting_start,
    update_task_due,
)

logger = logging.getLogger(__name__)

router = Router()

# Исходные фразы пользователя: "task:12" / "meet:5" -> текст. Нужны для смены типа.
# После рестарта пусто — карточка «устаревает».
_RAW_LIMIT = 200
_raw: dict[str, str] = {}

_STALE = "Карточка устарела — создай заново"


class CardFlow(StatesGroup):
    waiting_time = State()


# Кнопки главного меню не должны считаться вводом времени — их обработает menu.router
_MENU_BUTTONS = {kb.BTN_TODAY, kb.BTN_WEEK, kb.BTN_HABITS, kb.BTN_TASK, kb.BTN_MEET, kb.BTN_NOTE}


def _remember(kind: str, obj_id: int, raw: str) -> None:
    _raw[f"{kind}:{obj_id}"] = raw
    while len(_raw) > _RAW_LIMIT:
        _raw.pop(next(iter(_raw)))


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


# --- Рендер ---

def render_task_card(task: dict) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"✅ Задача: {task['text']}\n"
        f"📅 {_format_due(task.get('due_date'), task.get('due_time'))} · {task['domain']}"
    )
    if task.get("recurrence"):
        text += f"\n🔁 {human_recurrence(task['recurrence'])}"
    tid = task["id"]
    rows = [
        [_btn("🤝 Это встреча", f"card:tomeet:{tid}"), _btn("📝 Это заметка", f"card:tonote:task:{tid}")],
        [_btn("🕐 Изменить время", f"card:time:task:{tid}"), _btn("🗑 Удалить", f"card:del:task:{tid}")],
    ]
    if task.get("recurrence"):
        rows.append([_btn("🛑 Не повторять", f"card:norep:{tid}")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def render_meeting_card(meeting: dict) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"🤝 Встреча: {meeting['title']}\n"
        f"📅 {_format_dt(meeting['start_at'])} · {meeting['domain']}\n"
        f"🔔 Напомню за час"
    )
    mid = meeting["id"]
    rows = [
        [_btn("🧩 Это задача", f"card:totask:{mid}"), _btn("📝 Это заметка", f"card:tonote:meet:{mid}")],
        [_btn("🕐 Изменить время", f"card:time:meet:{mid}"), _btn("🗑 Удалить", f"card:del:meet:{mid}")],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def send_task_card(message: Message, task: dict, raw: str, prefix: str = "") -> None:
    _remember("task", task["id"], raw)
    text, markup = render_task_card(task)
    if prefix:
        text = f"{prefix}\n\n{text}"
    await message.answer(text, reply_markup=markup)


async def send_meeting_card(message: Message, meeting: dict, raw: str, prefix: str = "") -> None:
    _remember("meet", meeting["id"], raw)
    text, markup = render_meeting_card(meeting)
    if prefix:
        text = f"{prefix}\n\n{text}"
    await message.answer(text, reply_markup=markup)


# --- Хелперы ---

def _split_start(start_at: str) -> tuple[str, str]:
    """'2026-10-31 15:00:00' -> ('2026-10-31', '15:00')."""
    return start_at[:10], start_at[11:16]


async def _load(kind: str, obj_id: int) -> dict | None:
    return await (get_task(obj_id) if kind == "task" else get_meeting(obj_id))


def _render(kind: str, obj: dict) -> tuple[str, InlineKeyboardMarkup]:
    return render_task_card(obj) if kind == "task" else render_meeting_card(obj)


def _note_text(kind: str, obj: dict) -> str:
    return obj["text"] if kind == "task" else obj["title"]


async def _show(callback: CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    if callback.message:
        await callback.message.edit_text(text, reply_markup=markup)


# --- Callbacks ---

@router.callback_query(F.data.startswith("card:del:"))
async def cb_delete(callback: CallbackQuery) -> None:
    _, _, kind, sid = callback.data.split(":")
    kind = "task" if kind == "task" else "meet"
    obj = await _load(kind, int(sid))
    if obj is None:
        await callback.answer("Уже удалено или не найдено")
        return
    await (delete_task(obj["id"]) if kind == "task" else delete_meeting(obj["id"]))
    _raw.pop(f"{kind}:{sid}", None)
    await callback.answer("Удалено")
    await _show(callback, f"🗑 Удалено: {_note_text(kind, obj)}")


@router.callback_query(F.data.startswith("card:tonote:"))
async def cb_to_note(callback: CallbackQuery) -> None:
    _, _, kind, sid = callback.data.split(":")
    kind = "task" if kind == "task" else "meet"
    obj = await _load(kind, int(sid))
    if obj is None:
        await callback.answer("Уже удалено или не найдено")
        return
    text = _raw.pop(f"{kind}:{sid}", None) or _note_text(kind, obj)
    await (delete_task(obj["id"]) if kind == "task" else delete_meeting(obj["id"]))
    _, indexed = await save_note(text)
    await callback.answer("Готово")
    msg = "📝 Сохранил заметкой"
    if not indexed:
        msg += " (индексация не удалась — /reindex)"
    await _show(callback, msg)


@router.callback_query(F.data.startswith("card:tomeet:"))
async def cb_to_meeting(callback: CallbackQuery) -> None:
    task_id = int(callback.data.split(":")[2])
    task = await get_task(task_id)
    if task is None:
        await callback.answer("Уже удалено или не найдено")
        return
    raw = _raw.get(f"task:{task_id}")
    if raw is None:
        await callback.answer(_STALE, show_alert=True)
        return

    meeting = None
    parsed = await parse_message(raw)
    if parsed is not None:
        meeting = meeting_from_parsed(parsed)
    if meeting is None and task.get("due_date"):
        meeting = meeting_from_parsed({
            "text": task["text"],
            "domain": task["domain"],
            "due_date": task["due_date"],
            "due_time": task.get("due_time"),
        })
    if meeting is None:
        await callback.answer("Для встречи нужна дата — создай через «🤝 Встреча»", show_alert=True)
        return
    meeting["domain"] = task["domain"]

    await delete_task(task_id)
    mid = await add_meeting(meeting["title"], meeting["domain"], meeting["start_at"])
    _raw.pop(f"task:{task_id}", None)
    _remember("meet", mid, raw)
    await callback.answer("Теперь это встреча")
    text, markup = render_meeting_card({"id": mid, **meeting})
    await _show(callback, text, markup)


@router.callback_query(F.data.startswith("card:totask:"))
async def cb_to_task(callback: CallbackQuery) -> None:
    mid = int(callback.data.split(":")[2])
    meeting = await get_meeting(mid)
    if meeting is None:
        await callback.answer("Уже удалено или не найдено")
        return
    due_date, due_time = _split_start(meeting["start_at"])
    await delete_meeting(mid)
    task = await create_task_from_parsed({
        "text": meeting["title"],
        "domain": meeting["domain"],
        "due_date": due_date,
        "due_time": due_time,
        "recurrence": None,
    })
    raw = _raw.pop(f"meet:{mid}", None)
    if raw:
        _remember("task", task["id"], raw)
    await callback.answer("Теперь это задача")
    text, markup = render_task_card(task)
    await _show(callback, text, markup)


@router.callback_query(F.data.startswith("card:norep:"))
async def cb_no_repeat(callback: CallbackQuery) -> None:
    task_id = int(callback.data.split(":")[2])
    task = await get_task(task_id)
    if task is None:
        await callback.answer("Уже удалено или не найдено")
        return
    await clear_task_recurrence(task_id)
    task["recurrence"] = None
    await callback.answer("Повтор отключён")
    text, markup = render_task_card(task)
    await _show(callback, text, markup)


@router.callback_query(F.data.startswith("card:time:"))
async def cb_time(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, kind, sid = callback.data.split(":")
    kind = "task" if kind == "task" else "meet"
    if await _load(kind, int(sid)) is None:
        await callback.answer("Уже удалено или не найдено")
        return
    await state.set_state(CardFlow.waiting_time)
    await state.update_data(
        kind=kind,
        id=int(sid),
        card_message_id=callback.message.message_id if callback.message else None,
    )
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "🕐 Когда? Например: «завтра в 19:00», «в пятницу». (или /cancel)"
        )


@router.message(
    StateFilter(CardFlow.waiting_time), F.text, ~F.text.startswith("/"), ~F.text.in_(_MENU_BUTTONS)
)
async def on_new_time(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    kind, obj_id = data.get("kind"), data.get("id")
    obj = await _load(kind, obj_id) if kind and obj_id else None
    if obj is None:
        await state.clear()
        await message.answer("Уже удалено или не найдено.")
        return

    parsed = await parse_message(message.text or "")
    new_date = parsed.get("due_date") if parsed else None
    new_time = parsed.get("due_time") if parsed else None
    if not new_date and not new_time:
        await message.answer("Не понял время, попробуй ещё раз или /cancel")
        return

    if kind == "task":
        due_date = new_date or obj.get("due_date") or date.today().isoformat()
        await update_task_due(obj_id, due_date, new_time)  # только дата → время сбрасываем
        obj["due_date"], obj["due_time"] = due_date, new_time
    else:
        old_date, old_time = _split_start(obj["start_at"])
        start_at = f"{new_date or old_date} {new_time or old_time}:00"
        try:
            datetime.strptime(start_at, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            await message.answer("Не понял время, попробуй ещё раз или /cancel")
            return
        await update_meeting_start(obj_id, start_at)
        obj["start_at"] = start_at

    await state.clear()

    card_id = data.get("card_message_id")
    if card_id:
        try:
            await message.bot.edit_message_reply_markup(
                chat_id=message.chat.id, message_id=card_id, reply_markup=None
            )
        except Exception:
            pass  # сообщение могло быть удалено/изменено

    text, markup = _render(kind, obj)
    await message.answer(text, reply_markup=markup)
