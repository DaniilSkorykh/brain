"""Хендлеры /task, /today, /week — управление задачами через SQLite."""
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

from bot.tools.llm import parse_message
from bot.tools.schedule import WEEKDAYS_RU, next_occurrence
from db.database import (
    add_task,
    complete_task,
    get_overdue_recurring,
    get_task,
    get_today_tasks,
    get_week_tasks,
    move_overdue_to,
    rollover_unfinished,
    update_task_due,
)

logger = logging.getLogger(__name__)

router = Router()

def urgency_emoji(due_date: str | None) -> str:
    """Смайлик срочности по дате: 🔴 просрочено, 🟢 сегодня, 🟡 позже, ⚪ без срока."""
    if not due_date:
        return "⚪"
    today = date.today().isoformat()
    if due_date < today:
        return "🔴"
    if due_date == today:
        return "🟢"
    return "🟡"


# Допустимые домены
_DOMAINS = ("technored", "ministry", "personal")
_DOMAIN_PATTERN = re.compile(
    r"#(technored|ministry|personal)", re.IGNORECASE
)


def _parse_domain(text: str) -> tuple[str, str]:
    """Извлекает домен из #-тега, возвращает (домен, текст_без_тега)."""
    match = _DOMAIN_PATTERN.search(text)
    if match:
        domain = match.group(1).lower()
        cleaned = _DOMAIN_PATTERN.sub("", text)
        cleaned = re.sub(r"  +", " ", cleaned).strip()
        return domain, cleaned
    return "personal", text


def _parse_due(text: str) -> tuple[str | None, str]:
    """
    Ищет дату в тексте через dateparser.
    Возвращает (ISO-дата 'YYYY-MM-DD' | None, текст_без_даты).
    Берёт ПОСЛЕДНЕЕ найденное совпадение.
    """
    try:
        from dateparser.search import search_dates  # type: ignore[import]

        results = search_dates(
            text,
            languages=["ru"],
            settings={
                "PREFER_DATES_FROM": "future",
                "RETURN_AS_TIMEZONE_AWARE": False,
            },
        )
        if not results:
            return None, text

        # Берём последнее совпадение
        matched_str, parsed_dt = results[-1]
        iso_date = parsed_dt.strftime("%Y-%m-%d")

        # Убираем найденную подстроку из текста
        # Ищем последнее вхождение (на случай дублей)
        idx = text.rfind(matched_str)
        if idx != -1:
            cleaned = text[:idx] + text[idx + len(matched_str):]
        else:
            cleaned = text
        cleaned = re.sub(r"  +", " ", cleaned).strip()

        return iso_date, cleaned
    except Exception:
        logger.exception("dateparser failed on text: %r", text)
        return None, text


_WEEKDAYS_RU = WEEKDAYS_RU


def _format_due(due_date: str | None, due_time: str | None) -> str:
    """'2026-10-31', '21:00' -> 'Сб 31.10 21:00'; без срока -> 'без срока'."""
    if not due_date:
        return "без срока"
    try:
        d = date.fromisoformat(due_date)
        s = f"{_WEEKDAYS_RU[d.weekday()]} {d.strftime('%d.%m')}"
    except ValueError:
        s = due_date
    return f"{s} {due_time}" if due_time else s


def task_line(t: dict, week: bool = False) -> str:
    """Строка задачи для списков. Обычная: '🟢 21:00 текст (домен)'; week: '🟡 текст — 2026-10-31 21:00'."""
    emoji = urgency_emoji(t["due_date"])
    rec = " 🔁" if t.get("recurrence") else ""
    if week:
        when = f"{t['due_date']} {t['due_time']}" if t.get("due_time") else t["due_date"]
        return f"{emoji} {t['text']}{rec} — {when}"
    time_part = f"{t['due_time']} " if t.get("due_time") else ""
    return f"{emoji} {time_part}{t['text']}{rec} ({t['domain']})"


async def complete_task_and_repeat(task_id: int) -> dict | None:
    """Закрывает задачу; если она повторяющаяся — создаёт следующий экземпляр и возвращает его."""
    task = await get_task(task_id)
    if task and task.get("is_done"):
        return None  # повторное нажатие ✅ — следующий экземпляр уже создан
    await complete_task(task_id)
    if not task or not task.get("recurrence") or not task.get("due_date"):
        return None
    try:
        base = max(date.fromisoformat(task["due_date"]), date.today())
    except ValueError:
        return None
    nxt = next_occurrence(task["recurrence"], base)
    if nxt is None:
        return None
    new_id = await add_task(
        task["text"], task["domain"], nxt.isoformat(), task.get("due_time"), task["recurrence"]
    )
    return {
        "id": new_id,
        "text": task["text"],
        "domain": task["domain"],
        "due_date": nxt.isoformat(),
        "due_time": task.get("due_time"),
        "recurrence": task["recurrence"],
    }


def _next_hint(nxt: dict | None) -> str:
    return f" · 🔁 следующая: {_format_due(nxt['due_date'], nxt['due_time'])}" if nxt else ""


async def create_task_from_parsed(parsed: dict) -> dict:
    """Создаёт задачу из результата llm.parse_message."""
    task_id = await add_task(
        parsed["text"],
        parsed.get("domain") or "personal",
        parsed.get("due_date"),
        parsed.get("due_time"),
        parsed.get("recurrence"),
    )
    return {
        "id": task_id,
        "text": parsed["text"],
        "domain": parsed.get("domain") or "personal",
        "due_date": parsed.get("due_date"),
        "due_time": parsed.get("due_time"),
        "recurrence": parsed.get("recurrence"),
    }


async def create_task_from_text(raw: str) -> dict | None:
    """Парсит '#домен текст дата' → создаёт задачу. None, если нет текста задачи.

    Сначала LLM (parse_message), при неудаче — dateparser. Явный #домен приоритетнее LLM.
    Переиспользуется командой /task, FSM-флоу и голосовыми сообщениями.
    """
    raw = raw.strip()
    explicit = bool(_DOMAIN_PATTERN.search(raw))
    domain, remaining = _parse_domain(raw)
    if not remaining.strip():
        return None

    parsed = await parse_message(remaining)
    if parsed is not None:
        # kind игнорируем — пользователь явно создаёт задачу
        if explicit:
            parsed["domain"] = domain
        return await create_task_from_parsed(parsed)

    due_date, task_text = _parse_due(remaining)
    task_text = task_text.strip()
    if not task_text:
        return None
    return await create_task_from_parsed({
        "text": task_text, "domain": domain, "due_date": due_date,
        "due_time": None, "recurrence": None,
    })


@router.message(Command("task"))
async def cmd_task(message: Message, command: CommandObject) -> None:
    args = command.args
    if not args or not args.strip():
        await message.answer(
            "Использование: /task <текст> [дата] [#домен]\n\n"
            "Примеры:\n"
            "• /task позвонить подрядчику завтра\n"
            "• /task #technored дедлайн по REDWELD в пятницу\n"
            "• /task купить подарок 2026-06-20\n"
            "• /task #ministry подготовить материалы для встречи\n\n"
            "Домены: #technored, #ministry, #personal (по умолчанию personal)"
        )
        return

    task = await create_task_from_text(args.strip())
    if task is None:
        await message.answer(
            "Не удалось понять текст задачи. "
            "Напиши, пожалуйста, что именно нужно сделать.\n"
            "Например: /task позвонить подрядчику завтра"
        )
        return

    # Локальный импорт: cards.py импортирует tasks.py (избегаем цикла)
    from bot.handlers.cards import send_task_card

    await send_task_card(message, task, raw=args.strip())


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    tasks = await get_today_tasks()
    if not tasks:
        await message.answer("На сегодня задач нет 🎉")
        return

    lines = ["Задачи на сегодня и просроченные:"]
    for t in tasks:
        lines.append(task_line(t))
    await message.answer("\n".join(lines))


@router.message(Command("week"))
async def cmd_week(message: Message) -> None:
    tasks = await get_week_tasks()
    if not tasks:
        await message.answer("На неделю задач нет")
        return

    # Группируем по домену в Python
    groups: dict[str, list[dict]] = {}
    for t in tasks:
        domain = t["domain"]
        groups.setdefault(domain, []).append(t)

    # Порядок доменов: technored → ministry → personal → прочие
    ordered_domains = [d for d in ("technored", "ministry", "personal") if d in groups]
    for d in groups:
        if d not in ordered_domains:
            ordered_domains.append(d)

    lines = ["Задачи на ближайшие 7 дней:"]
    for domain in ordered_domains:
        lines.append(f"\n[{domain}]")
        for t in groups[domain]:
            lines.append("  " + task_line(t, week=True))

    await message.answer("\n".join(lines))


# --- Вечерняя сводка: список с кнопками ✅ и переносом на завтра ---

def _short(text: str, limit: int = 30) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def build_evening_view() -> tuple[str, InlineKeyboardMarkup | None]:
    """Текст и клавиатура вечерней сводки по незакрытым задачам на сегодня."""
    tasks = await get_today_tasks()
    if not tasks:
        return "🌙 Вечер. Все задачи на сегодня закрыты — красавчик! 🎉", None

    lines = ["🌙 Итоги дня. Что закрыто? Отметь ✅, остальное перенесём на завтра:", ""]
    rows: list[list[InlineKeyboardButton]] = []
    for t in tasks:
        lines.append(task_line(t))
        rows.append([
            InlineKeyboardButton(
                text=f"✅ {_short(t['text'])}",
                callback_data=f"done:{t['id']}",
            )
        ])
    rows.append([
        InlineKeyboardButton(
            text="⏭ Перенести остальные на завтра",
            callback_data="rollover",
        )
    ])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("done:"))
async def cb_done(callback: CallbackQuery) -> None:
    task_id = int(callback.data.split(":", 1)[1])
    nxt = await complete_task_and_repeat(task_id)
    await callback.answer("Готово ✅" + _next_hint(nxt))
    text, markup = await build_evening_view()
    if callback.message:
        await callback.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data.startswith("tdone:"))
async def cb_task_reminder_done(callback: CallbackQuery) -> None:
    """Кнопка «✅ Сделано» под напоминанием о задаче."""
    task_id = int(callback.data.split(":", 1)[1])
    nxt = await complete_task_and_repeat(task_id)
    await callback.answer("Готово ✅" + _next_hint(nxt))
    if callback.message and callback.message.text:
        await callback.message.edit_text(callback.message.text + "\n\n✅ Сделано", reply_markup=None)


@router.callback_query(F.data == "rollover")
async def cb_rollover(callback: CallbackQuery) -> None:
    today = date.today()
    tomorrow = (today + timedelta(days=1)).isoformat()
    moved = await rollover_unfinished(tomorrow)
    # Повторяющиеся не переносим на завтра, а «перешагиваем» на ближайшую дату строго после сегодня
    for t in await get_overdue_recurring(tomorrow):  # срок <= сегодня
        nxt = next_occurrence(t["recurrence"], today)
        if nxt:
            await update_task_due(t["id"], nxt.isoformat(), t.get("due_time"))
            moved += 1
    await callback.answer(f"Перенесено: {moved}")
    if callback.message:
        await callback.message.edit_text(
            f"⏭ Перенёс на завтра задач: {moved}.\nЗавтра утром напомню. Спокойной ночи 🌙"
        )


@router.callback_query(F.data == "weekmove")
async def cb_weekmove(callback: CallbackQuery) -> None:
    """Итоги недели: просроченное → на ближайший понедельник (повторяющиеся — на след. дату)."""
    today = date.today()
    monday = today + timedelta(days=7 - today.weekday())  # строго следующий понедельник
    moved = await move_overdue_to(monday.isoformat())
    for t in await get_overdue_recurring(today.isoformat()):
        nxt = next_occurrence(t["recurrence"], today)
        if nxt:
            await update_task_due(t["id"], nxt.isoformat(), t.get("due_time"))
            moved += 1
    await callback.answer(f"Перенесено: {moved}")
    if callback.message and callback.message.text:
        await callback.message.edit_text(
            callback.message.text + f"\n\n⏭ Перенёс: {moved}", reply_markup=None
        )
