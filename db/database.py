"""Async CRUD-слой для SQLite через aiosqlite."""
from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

from bot.config import settings

logger = logging.getLogger(__name__)

DB_PATH = settings.sqlite_path

# Путь к схеме относительно этого файла
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


async def init_db() -> None:
    """Создаёт папку и таблицы (идемпотентно)."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    schema = _SCHEMA_PATH.read_text(encoding="utf-8")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(schema)
        # Миграция: новые колонки tasks для уже существующих БД
        cursor = await db.execute("PRAGMA table_info(tasks)")
        existing = {row[1] for row in await cursor.fetchall()}
        for name, ddl in (
            ("due_time", "TEXT"),
            ("reminder_sent", "BOOLEAN DEFAULT 0"),
            ("recurrence", "TEXT"),
        ):
            if name not in existing:
                await db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")
                logger.info("Миграция: добавлена колонка tasks.%s", name)
        await db.commit()
    logger.info("Database initialised at %s", DB_PATH)


async def add_task(
    text: str,
    domain: str = "personal",
    due_date: str | None = None,
    due_time: str | None = None,
    recurrence: str | None = None,
) -> int:
    """Вставляет задачу, возвращает id новой строки."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO tasks (text, domain, due_date, due_time, recurrence) "
            "VALUES (?, ?, ?, ?, ?)",
            (text, domain, due_date, due_time, recurrence),
        )
        await db.commit()
        return cursor.lastrowid  # type: ignore[return-value]


async def get_task(task_id: int) -> dict | None:
    """Задача по id или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, text, domain, due_date, due_time, recurrence, is_done "
            "FROM tasks WHERE id = ?",
            (task_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def delete_task(task_id: int) -> bool:
    """Удаляет задачу. True если строка удалена."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await db.commit()
        return cursor.rowcount > 0


async def update_task_due(task_id: int, due_date: str | None, due_time: str | None) -> bool:
    """Меняет срок задачи и сбрасывает признак отправленного напоминания."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE tasks SET due_date = ?, due_time = ?, reminder_sent = 0 WHERE id = ?",
            (due_date, due_time, task_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_due_task_reminders(now_iso: str) -> list[dict]:
    """Задачи со временем, которым пора напомнить (now_iso — 'YYYY-MM-DD HH:MM').

    Старше 1 дня не берём — чтобы после миграции/простоя не прилетела пачка старья.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, text, domain, due_date, due_time, recurrence
            FROM tasks
            WHERE is_done = 0
              AND due_date IS NOT NULL
              AND due_time IS NOT NULL
              AND reminder_sent = 0
              AND due_date || ' ' || due_time <= substr(?, 1, 16)
              AND due_date >= date(substr(?, 1, 10), '-1 day')
            ORDER BY due_date ASC, due_time ASC
            """,
            (now_iso, now_iso),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def mark_task_reminded(task_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE tasks SET reminder_sent = 1 WHERE id = ?", (task_id,))
        await db.commit()


async def get_today_tasks() -> list[dict]:
    """Незавершённые задачи на сегодня и просроченные."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, text, domain, due_date, due_time, recurrence
            FROM tasks
            WHERE is_done = 0
              AND due_date IS NOT NULL
              AND due_date <= date('now', 'localtime')
            ORDER BY due_date ASC, due_time IS NULL, due_time ASC
            """
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_week_tasks() -> list[dict]:
    """Незавершённые задачи на ближайшие 7 дней (включая сегодня)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, text, domain, due_date, due_time, recurrence
            FROM tasks
            WHERE is_done = 0
              AND due_date IS NOT NULL
              AND due_date >= date('now', 'localtime')
              AND due_date <= date('now', 'localtime', '+7 days')
            ORDER BY domain ASC, due_date ASC, due_time IS NULL, due_time ASC
            """
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def add_meeting(title: str, domain: str, start_at: str) -> int:
    """Добавляет встречу (start_at — 'YYYY-MM-DD HH:MM:SS' локального времени)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO meetings (title, domain, start_at) VALUES (?, ?, ?)",
            (title, domain, start_at),
        )
        await db.commit()
        return cursor.lastrowid  # type: ignore[return-value]


async def get_meeting(meeting_id: int) -> dict | None:
    """Встреча по id или None."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, title, domain, start_at FROM meetings WHERE id = ?",
            (meeting_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def delete_meeting(meeting_id: int) -> bool:
    """Удаляет встречу. True если строка удалена."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        await db.commit()
        return cursor.rowcount > 0


async def update_meeting_start(meeting_id: int, start_at: str) -> bool:
    """Меняет время встречи и сбрасывает признак отправленного напоминания."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE meetings SET start_at = ?, reminder_sent = 0 WHERE id = ?",
            (start_at, meeting_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def clear_task_recurrence(task_id: int) -> bool:
    """Убирает повторение у задачи."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE tasks SET recurrence = NULL WHERE id = ?", (task_id,)
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_due_meetings(now_iso: str, limit_iso: str) -> list[dict]:
    """Встречи, до которых <= окна напоминания и которые ещё не напоминались."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, title, domain, start_at
            FROM meetings
            WHERE reminder_sent = 0
              AND start_at > ?
              AND start_at <= ?
            ORDER BY start_at ASC
            """,
            (now_iso, limit_iso),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def mark_meeting_reminded(meeting_id: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE meetings SET reminder_sent = 1 WHERE id = ?",
            (meeting_id,),
        )
        await db.commit()


async def get_upcoming_meetings(limit: int = 10) -> list[dict]:
    """Ближайшие будущие встречи."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT id, title, domain, start_at
            FROM meetings
            WHERE start_at >= datetime('now', 'localtime')
            ORDER BY start_at ASC
            LIMIT ?
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def rollover_unfinished(to_date: str) -> int:
    """Переносит невыполненные задачи (срок <= сегодня) на дату to_date. Возвращает число перенесённых."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE tasks
            SET due_date = ?, reminder_sent = 0
            WHERE is_done = 0
              AND due_date IS NOT NULL
              AND due_date <= date('now', 'localtime')
            """,
            (to_date,),
        )
        await db.commit()
        return cursor.rowcount


async def add_habit(title: str, domain: str, schedule: str) -> int:
    """Добавляет привычку. schedule: 'daily' или 'wd:0,2,4' (Пн=0)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "INSERT INTO habits (title, domain, schedule) VALUES (?, ?, ?)",
            (title, domain, schedule),
        )
        await db.commit()
        return cursor.lastrowid  # type: ignore[return-value]


async def get_active_habits() -> list[dict]:
    """Все активные привычки."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, title, domain, schedule FROM habits WHERE active = 1 ORDER BY id ASC"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_habits_due(weekday: int) -> list[dict]:
    """Привычки, запланированные на указанный день недели (Пн=0): daily или с этим днём."""
    habits = await get_active_habits()
    due = []
    for h in habits:
        sched = h["schedule"]
        if sched == "daily":
            due.append(h)
        elif sched.startswith("wd:"):
            days = sched[3:].split(",")
            if str(weekday) in days:
                due.append(h)
    return due


async def log_habit(habit_id: int, done_date: str) -> None:
    """Отмечает привычку выполненной за дату (идемпотентно)."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO habit_log (habit_id, done_date) VALUES (?, ?)",
            (habit_id, done_date),
        )
        await db.commit()


async def get_habit_log_dates(habit_id: int) -> list[str]:
    """Все даты отметок привычки (ISO, по убыванию)."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT done_date FROM habit_log WHERE habit_id = ? ORDER BY done_date DESC",
            (habit_id,),
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]


async def deactivate_habit(habit_id: int) -> bool:
    """Архивирует привычку (active=0). True если строка обновлена."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE habits SET active = 0 WHERE id = ? AND active = 1",
            (habit_id,),
        )
        await db.commit()
        return cursor.rowcount > 0


async def complete_task(task_id: int) -> bool:
    """Помечает задачу выполненной. Возвращает True если строка была обновлена."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            """
            UPDATE tasks
            SET is_done = 1, completed_at = CURRENT_TIMESTAMP
            WHERE id = ? AND is_done = 0
            """,
            (task_id,),
        )
        await db.commit()
        return cursor.rowcount > 0
