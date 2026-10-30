"""Планировщик фоновых задач: утренний дайджест, вечерняя сводка, напоминания о встречах и задачах."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from bot.config import settings
from bot.handlers.habits import _week_for
from bot.handlers.tasks import build_evening_view, task_line
from db.database import (
    count_done_tasks_between,
    get_active_habits,
    get_meetings_between,
    get_overdue_tasks,
    get_tasks_between,
    get_due_meetings,
    get_due_task_reminders,
    get_habits_due,
    get_today_tasks,
    mark_meeting_reminded,
    mark_task_reminded,
)

logger = logging.getLogger(__name__)


def _parse_hm(value: str) -> tuple[int, int]:
    """'09:00' -> (9, 0). При ошибке — (9, 0)."""
    try:
        hh, mm = value.split(":")
        return int(hh), int(mm)
    except Exception:
        logger.warning("Не удалось разобрать время %r, использую 09:00", value)
        return 9, 0


async def send_morning_digest(bot: Bot) -> None:
    """Утром: задачи на сегодня и просроченные + привычки на сегодня."""
    uid = settings.telegram_allowed_user_id
    tasks = await get_today_tasks()

    lines = ["☀️ Доброе утро, Даниил!", ""]
    if tasks:
        lines.append("Задачи на сегодня:")
        for t in tasks:
            lines.append(task_line(t))
        lines.append("\n🔴 просрочено · 🟢 сегодня")
    else:
        lines.append("На сегодня задач нет 🎉")

    await bot.send_message(uid, "\n".join(lines))

    # Привычки на сегодня — отдельным сообщением с кнопками отметки
    habits = await get_habits_due(date.today().weekday())
    if habits:
        hlines = ["🔁 Привычки на сегодня:"]
        rows: list[list[InlineKeyboardButton]] = []
        for h in habits:
            week = await _week_for(h["id"], h["schedule"])
            hlines.append(f"• {h['title']} {week}")
            rows.append([
                InlineKeyboardButton(
                    text=f"✅ {h['title'][:30]}",
                    callback_data=f"habdone:{h['id']}",
                )
            ])
        await bot.send_message(
            uid, "\n".join(hlines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )


async def send_evening_review(bot: Bot) -> None:
    """Вечером: список незакрытых задач с кнопками ✅ и переносом на завтра."""
    uid = settings.telegram_allowed_user_id
    text, markup = await build_evening_view()
    await bot.send_message(uid, text, reply_markup=markup)


async def send_weekly_review(bot: Bot) -> None:
    """Итоги недели без LLM: закрытое, просрочка, привычки, план на следующую неделю."""
    uid = settings.telegram_allowed_user_id
    today = date.today()
    start = today - timedelta(days=6)
    today_iso = today.isoformat()

    done = await count_done_tasks_between(start.isoformat(), today_iso)
    overdue = await get_overdue_tasks(today_iso)

    lines = [
        f"📊 Итоги недели ({start.strftime('%d.%m')}–{today.strftime('%d.%m')})",
        f"✅ Закрыто задач: {done}",
        f"🔴 Просрочено: {len(overdue)}",
    ]
    for t in overdue[:5]:
        lines.append("  " + task_line(t))
    if len(overdue) > 5:
        lines.append(f"  … и ещё {len(overdue) - 5}")

    habits = await get_active_habits()
    if habits:
        lines.append("🔁 Привычки:")
        for h in habits:
            lines.append(f"• {h['title']} {await _week_for(h['id'], h['schedule'])}")

    nxt_start = (today + timedelta(days=1)).isoformat()
    nxt_end = (today + timedelta(days=7)).isoformat()
    tasks = await get_tasks_between(nxt_start, nxt_end)
    meetings = await get_meetings_between(nxt_start, nxt_end)
    lines.append(f"🗓 На следующей неделе — задач: {len(tasks)}, встреч: {len(meetings)}")
    for t in tasks[:7]:
        lines.append("  " + task_line(t, week=True))
    if len(tasks) > 7:
        lines.append(f"  … и ещё {len(tasks) - 7}")
    for m in meetings:
        try:
            when = datetime.strptime(m["start_at"], "%Y-%m-%d %H:%M:%S").strftime("%d.%m %H:%M")
        except ValueError:
            when = m["start_at"]
        lines.append(f"  📅 {m['title']} — {when}")

    markup = None
    if overdue:
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="⏭ Перенести просроченное на понедельник", callback_data="weekmove"
            )
        ]])
    await bot.send_message(uid, "\n".join(lines), reply_markup=markup)


async def check_reminders(bot: Bot) -> None:
    """Каждую минуту: напоминания о встречах (за окно до начала) и о задачах со временем."""
    uid = settings.telegram_allowed_user_id
    now = datetime.now()
    now_iso = now.strftime("%Y-%m-%d %H:%M:%S")
    limit_iso = (now + timedelta(minutes=settings.meeting_reminder_min)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    for m in await get_due_meetings(now_iso, limit_iso):
        try:
            when = datetime.strptime(m["start_at"], "%Y-%m-%d %H:%M:%S").strftime("%H:%M")
        except Exception:
            when = m["start_at"]
        await bot.send_message(uid, f"🔔 Через час встреча: {m['title']} — в {when}")
        await mark_meeting_reminded(m["id"])

    task_now = now.strftime("%Y-%m-%d %H:%M")
    for t in await get_due_task_reminders(task_now):
        try:
            suffix = " (🔁)" if t.get("recurrence") else ""
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Сделано", callback_data=f"tdone:{t['id']}")
            ]])
            await bot.send_message(uid, f"⏰ Напоминаю: {t['text']}{suffix}", reply_markup=kb)
            await mark_task_reminded(t["id"])
        except Exception:
            logger.exception("Не удалось отправить напоминание о задаче %s", t["id"])


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    """Создаёт и запускает планировщик с регулярными задачами."""
    scheduler = AsyncIOScheduler(timezone=settings.timezone)

    m_h, m_m = _parse_hm(settings.morning_time)
    scheduler.add_job(
        send_morning_digest,
        CronTrigger(hour=m_h, minute=m_m, timezone=settings.timezone),
        args=[bot],
        id="morning_digest",
        replace_existing=True,
    )

    e_h, e_m = _parse_hm(settings.evening_time)
    scheduler.add_job(
        send_evening_review,
        CronTrigger(hour=e_h, minute=e_m, timezone=settings.timezone),
        args=[bot],
        id="evening_review",
        replace_existing=True,
    )

    w_h, w_m = _parse_hm(settings.weekly_review_time)
    scheduler.add_job(
        send_weekly_review,
        CronTrigger(
            day_of_week=settings.weekly_review_day, hour=w_h, minute=w_m,
            timezone=settings.timezone,
        ),
        args=[bot],
        id="weekly_review",
        replace_existing=True,
    )

    scheduler.add_job(
        check_reminders,
        IntervalTrigger(minutes=1),
        args=[bot],
        id="meeting_reminders",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(
        "Планировщик запущен (TZ=%s): утро %02d:%02d, вечер %02d:%02d, "
        "итоги недели %s %02d:%02d, напоминания о встречах за %d мин",
        settings.timezone, m_h, m_m, e_h, e_m,
        settings.weekly_review_day, w_h, w_m, settings.meeting_reminder_min,
    )
    return scheduler
