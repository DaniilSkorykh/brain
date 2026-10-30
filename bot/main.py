import asyncio
import logging
from urllib.parse import urlsplit

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand

from bot.config import settings
from bot.handlers import (
    admin,
    ask,
    cards,
    extract,
    files,
    habits,
    meetings,
    menu,
    notes,
    tasks,
    voice,
)
from bot.middlewares.access import AccessMiddleware
from bot.scheduler import setup_scheduler
from db.database import init_db


# Нативное меню команд Telegram (кнопка "Menu" + автоподсказка по "/").
BOT_COMMANDS = [
    BotCommand(command="note", description="Сохранить заметку в inbox"),
    BotCommand(command="ask", description="Спросить по базе знаний"),
    BotCommand(command="reindex", description="Переиндексировать базу знаний"),
    BotCommand(command="task", description="Добавить задачу [дата] [#домен]"),
    BotCommand(command="today", description="Задачи на сегодня и просроченные"),
    BotCommand(command="week", description="Обзор задач на 7 дней"),
    BotCommand(command="habit", description="Добавить привычку [расписание]"),
    BotCommand(command="habits", description="Привычки и стрики"),
    BotCommand(command="meet", description="Добавить встречу (напомню за час)"),
    BotCommand(command="meetings", description="Ближайшие встречи"),
    BotCommand(command="extract", description="Извлечь задачи из текста встречи"),
    BotCommand(command="weekly", description="Итоги недели"),
    BotCommand(command="help", description="Список команд"),
]


async def set_bot_commands(bot: Bot) -> None:
    await bot.set_my_commands(BOT_COMMANDS)


def build_bot() -> Bot:
    """Создаёт Bot. В local_bot_api режиме ходит через self-hosted Bot API (лимит 2 ГБ)."""
    if settings.local_bot_api and settings.bot_api_base_url:
        from aiogram.client.session.aiohttp import AiohttpSession
        from aiogram.client.telegram import TelegramAPIServer

        session = AiohttpSession(
            api=TelegramAPIServer.from_base(settings.bot_api_base_url, is_local=True)
        )
        logging.info("Использую локальный Bot API: %s", settings.bot_api_base_url)
        return Bot(token=settings.telegram_bot_token, session=session)
    return Bot(token=settings.telegram_bot_token)


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.middleware(AccessMiddleware())
    # Порядок важен: menu.router (кнопки/FSM) перед ask; ask.router — последним
    # (catch-all на свободный текст).
    dp.include_router(notes.router)
    dp.include_router(admin.router)
    dp.include_router(tasks.router)
    dp.include_router(habits.router)
    dp.include_router(meetings.router)
    dp.include_router(cards.router)
    dp.include_router(menu.router)
    dp.include_router(extract.router)
    dp.include_router(files.router)
    dp.include_router(voice.router)
    dp.include_router(ask.router)
    return dp


async def run_polling() -> None:
    await init_db()
    bot = build_bot()
    dp = build_dispatcher()
    await set_bot_commands(bot)
    setup_scheduler(bot)
    await dp.start_polling(bot)


async def run_webhook() -> None:
    from aiohttp import web
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

    await init_db()
    bot = build_bot()
    dp = build_dispatcher()

    setup_scheduler(bot)

    parsed = urlsplit(settings.telegram_webhook_url)
    path = parsed.path if parsed.path else "/webhook"

    await set_bot_commands(bot)

    app = web.Application()
    SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=path)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=8443)
    await site.start()

    # Регистрируем webhook только когда сервер уже слушает, иначе
    # первые апдейты от Telegram уйдут в никуда.
    await bot.set_webhook(settings.telegram_webhook_url)

    logging.info("Webhook server started on 0.0.0.0:8443, path=%s", path)

    # Run forever
    await asyncio.Event().wait()


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    if settings.bot_mode == "webhook":
        asyncio.run(run_webhook())
    else:
        asyncio.run(run_polling())


if __name__ == "__main__":
    main()
