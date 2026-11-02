"""Пути для файлов, которые создаёт бот."""
from __future__ import annotations

from pathlib import Path

from bot.config import settings


def bot_output_dir(domain: str) -> Path:
    """knowledge_dir / bot_inbox_dir / domain, создаёт папку."""
    path = Path(settings.knowledge_dir) / settings.bot_inbox_dir / domain
    path.mkdir(parents=True, exist_ok=True)
    return path
