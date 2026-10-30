"""Общий код расписаний (привычки и повторяющиеся задачи).

Формат: 'daily' | 'wd:0,2,4' (Пн=0..Вс=6) | 'monthly:N' (N-е число; в коротком месяце — последний день).
"""
from __future__ import annotations

import calendar
from datetime import date, timedelta

WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _wd_days(schedule: str) -> list[int]:
    return [int(d) for d in schedule[3:].split(",") if d.isdigit() and int(d) < 7]


def is_scheduled(day: date, schedule: str) -> bool:
    if schedule == "daily":
        return True
    if schedule.startswith("wd:"):
        return day.weekday() in _wd_days(schedule)
    if schedule.startswith("monthly:"):
        try:
            n = int(schedule[8:])
        except ValueError:
            return False
        last = calendar.monthrange(day.year, day.month)[1]
        return day.day == min(n, last)
    return False


def human_schedule(schedule: str | None) -> str:
    """'daily' -> 'каждый день', 'wd:0,2' -> 'Пн, Ср', 'monthly:1' -> 'каждое 1-е число'."""
    if not schedule:
        return ""
    if schedule == "daily":
        return "каждый день"
    if schedule.startswith("wd:"):
        days = _wd_days(schedule)
        if days == [0, 1, 2, 3, 4]:
            return "по будням"
        return ", ".join(WEEKDAYS_RU[d] for d in days)
    if schedule.startswith("monthly:"):
        return f"каждое {schedule[8:]}-е число"
    return schedule


def next_occurrence(schedule: str, after: date) -> date | None:
    """Ближайшая дата строго после after по расписанию (поиск на 62 дня вперёд)."""
    for i in range(1, 63):
        day = after + timedelta(days=i)
        if is_scheduled(day, schedule):
            return day
    return None
