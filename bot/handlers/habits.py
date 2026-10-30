"""Привычки / регулярные дела со стрик-счётчиками.

Регистрируется ДО ask.router. Расписание хранится как 'daily' или 'wd:0,2,4' (Пн=0).
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.handlers.tasks import _parse_domain
from bot.tools.schedule import human_schedule, is_scheduled
from db.database import (
    add_habit,
    deactivate_habit,
    get_active_habits,
    get_habit_log_dates,
    log_habit,
)

logger = logging.getLogger(__name__)

router = Router()

# Токены дней недели → индекс (Пн=0). Покрываем сокращения и полные формы.
_WD_TOKENS = {
    "пн": 0, "пон": 0, "понедельник": 0,
    "вт": 1, "вторник": 1,
    "ср": 2, "среда": 2, "среду": 2,
    "чт": 3, "четверг": 3,
    "пт": 4, "пятница": 4, "пятницу": 4,
    "сб": 5, "суббота": 5, "субботу": 5,
    "вс": 6, "воскресенье": 6,
}


def _parse_schedule(text: str) -> tuple[str, str]:
    """Извлекает расписание из текста. Возвращает (schedule, текст_без_расписания).

    schedule: 'daily' | 'wd:0,2,4'. По умолчанию — 'daily'.
    """
    low = text.lower()
    removed_spans: list[str] = []

    # Групповые ключевые слова
    if re.search(r"по\s+будн|будн|рабочие дни", low):
        for m in re.finditer(r"по\s+будням|будн\w*|рабочие дни", low):
            removed_spans.append(m.group(0))
        cleaned = _strip_tokens(text, removed_spans)
        return "wd:0,1,2,3,4", cleaned

    if re.search(r"по\s+выходн|выходн", low):
        for m in re.finditer(r"по\s+выходным|выходн\w*", low):
            removed_spans.append(m.group(0))
        cleaned = _strip_tokens(text, removed_spans)
        return "wd:5,6", cleaned

    if re.search(r"кажд\w*\s+день|ежедневн", low):
        for m in re.finditer(r"кажд\w*\s+день|ежедневн\w*", low):
            removed_spans.append(m.group(0))
        cleaned = _strip_tokens(text, removed_spans)
        return "daily", cleaned

    # Конкретные дни недели
    days: set[int] = set()
    for token, idx in _WD_TOKENS.items():
        if re.search(rf"\b{token}\b", low):
            days.add(idx)
            removed_spans.append(token)
    if days:
        cleaned = _strip_tokens(text, removed_spans)
        sched = "wd:" + ",".join(str(d) for d in sorted(days))
        return sched, cleaned

    return "daily", text


def _strip_tokens(text: str, tokens: list[str]) -> str:
    """Удаляет перечисленные подстроки (без регистра) и лишние разделители."""
    result = text
    for tok in tokens:
        result = re.sub(re.escape(tok), "", result, flags=re.IGNORECASE)
    # Чистим висящие предлоги/разделители и двойные пробелы
    result = re.sub(r"\bпо\b", "", result, flags=re.IGNORECASE)
    result = re.sub(r"[\s,;–—-]+", " ", result).strip(" ,;–—-")
    return result


def streak(log_dates: list[str], schedule: str, today: date | None = None) -> int:
    """Считает текущий стрик: подряд идущие запланированные дни с отметкой.

    Сегодня без отметки не ломает стрик (день ещё не закончился) — просто не считается.
    """
    today = today or date.today()
    done = set(log_dates)
    count = 0
    day = today
    # Ограничение на случай пустого/битого расписания
    for _ in range(366):
        if is_scheduled(day, schedule):
            iso = day.isoformat()
            if iso in done:
                count += 1
            elif day == today:
                pass  # сегодня ещё можно отметить — не ломаем
            else:
                break
        day -= timedelta(days=1)
    return count


def create_habit_from_text(raw: str) -> dict | None:
    """Парсит '#домен текст расписание' → создаёт привычку. None, если нет названия."""
    domain, remaining = _parse_domain(raw.strip())
    schedule, title = _parse_schedule(remaining)
    title = title.strip()
    if not title:
        return None
    return {"domain": domain, "title": title, "schedule": schedule}


def habit_button(habit_id: int) -> InlineKeyboardButton:
    """Кнопка отметки привычки (для дайджеста и /habits)."""
    return InlineKeyboardButton(text="✅ Сделал", callback_data=f"habdone:{habit_id}")


def week_marks(log_dates: list[str], schedule: str, today: date | None = None) -> str:
    """Последние 7 дней: ✅ отмечено, ▫ пропуск, · не по расписанию. В конце ' X/Y'."""
    today = today or date.today()
    done = set(log_dates)
    marks: list[str] = []
    x = y = 0
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        if is_scheduled(day, schedule):
            y += 1
            if day.isoformat() in done:
                x += 1
                marks.append("✅")
            else:
                marks.append("▫")
        else:
            marks.append("·")
    return f"{''.join(marks)} {x}/{y}"


async def _week_for(habit_id: int, schedule: str) -> str:
    dates = await get_habit_log_dates(habit_id)
    return week_marks(dates, schedule)


async def _streak_for(habit_id: int, schedule: str) -> int:
    dates = await get_habit_log_dates(habit_id)
    return streak(dates, schedule)


@router.message(Command("habit"))
async def cmd_habit(message: Message, command: CommandObject) -> None:
    args = command.args
    if not args or not args.strip():
        await message.answer(
            "Использование: /habit <название> [расписание] [#домен]\n\n"
            "Примеры:\n"
            "• /habit чтение Библии каждый день\n"
            "• /habit гитара пн ср пт\n"
            "• /habit планёрка по будням #technored\n"
            "• /habit разбор недели вс #ministry\n\n"
            "Без расписания — каждый день. Отмечать буду в утреннем дайджесте."
        )
        return

    habit = create_habit_from_text(args.strip())
    if habit is None:
        await message.answer("Не понял название привычки. Например: /habit зарядка каждый день")
        return

    hid = await add_habit(habit["title"], habit["domain"], habit["schedule"])
    await message.answer(
        f"🔁 Привычка #{hid} добавлена\n"
        f"Что: {habit['title']}\n"
        f"Когда: {human_schedule(habit['schedule'])}\n"
        f"Домен: {habit['domain']}"
    )


@router.message(Command("habits"))
async def cmd_habits(message: Message) -> None:
    habits = await get_active_habits()
    if not habits:
        await message.answer(
            "Привычек пока нет.\n"
            "Добавь: /habit чтение Библии каждый день"
        )
        return

    today_wd = date.today().weekday()
    lines = ["🔁 Твои привычки:"]
    rows: list[list[InlineKeyboardButton]] = []
    for h in habits:
        week = await _week_for(h["id"], h["schedule"])
        due_today = is_scheduled(date.today(), h["schedule"])
        mark = " · сегодня" if due_today else ""
        lines.append(f"• {h['title']} ({human_schedule(h['schedule'])}) {week}{mark}")
        if due_today:
            rows.append([
                InlineKeyboardButton(
                    text=f"✅ {h['title'][:30]}",
                    callback_data=f"habdone:{h['id']}",
                )
            ])
    markup = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    await message.answer("\n".join(lines), reply_markup=markup)


@router.callback_query(F.data.startswith("habdone:"))
async def cb_habit_done(callback: CallbackQuery) -> None:
    habit_id = int(callback.data.split(":", 1)[1])
    await log_habit(habit_id, date.today().isoformat())

    # Найдём расписание привычки для актуального стрика
    sched = "daily"
    title = ""
    for h in await get_active_habits():
        if h["id"] == habit_id:
            sched = h["schedule"]
            title = h["title"]
            break
    week = week_marks(await get_habit_log_dates(habit_id), sched)
    await callback.answer(f"✅ Отмечено · {week.rsplit(' ', 1)[1]} за неделю")
    if callback.message and title:
        # Тихо подтверждаем в чате отдельным сообщением (не ломаем дайджест-список)
        await callback.message.answer(f"✅ {title} {week}")
