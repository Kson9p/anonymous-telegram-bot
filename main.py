"""Точка входа Telegram-бота для двусторонней переписки с владельцем."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramConflictError,
    TelegramForbiddenError,
    TelegramUnauthorizedError,
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
    ErrorEvent,
)

from config import ConfigError, Settings, load_settings
from database import Database, DatabaseError
from handlers.admin import create_admin_router
from handlers.user import create_user_router
from services.media_groups import MediaGroupBuffer
from services.middleware import UpdateDeduperMiddleware
from services.rate_limit import NoticeThrottle, RateLimiter
from services.relay import RelayService

logger = logging.getLogger(__name__)


def _configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        force=True,
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


async def _setup_bot_commands(bot: Bot, owner_id: int) -> None:
    user_commands = [
        BotCommand(command="start", description="Начать"),
        BotCommand(command="help", description="Как это работает"),
    ]
    owner_commands = [
        BotCommand(command="menu", description="Панель управления"),
        BotCommand(command="cancel", description="Отменить ответ"),
        BotCommand(command="ban", description="Заблокировать пользователя"),
        BotCommand(command="unban", description="Разблокировать пользователя"),
        BotCommand(command="help", description="Команды владельца"),
    ]
    try:
        await bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())
        await bot.set_my_commands(
            owner_commands, scope=BotCommandScopeChat(chat_id=owner_id)
        )
    except TelegramAPIError as exc:
        logger.warning("Не удалось обновить меню команд: %s", type(exc).__name__)


async def _cleanup_database(database: Database, settings: Settings) -> None:
    now = int(time.time())
    try:
        deleted = await database.cleanup_old_data(
            history_cutoff=now - settings.history_retention_days * 86_400,
            updates_cutoff=now - 7 * 86_400,
        )
    except DatabaseError:
        logger.error("Очистка базы данных не выполнена")
        return
    logger.info(
        "Очистка базы: сообщений=%s, привязок=%s, update=%s",
        deleted["messages"],
        deleted["owner_mappings"],
        deleted["processed_updates"],
    )


async def _maintenance_loop(
    database: Database,
    settings: Settings,
    stop_event: asyncio.Event,
) -> None:
    while True:
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=settings.maintenance_interval_seconds,
            )
            return
        except TimeoutError:
            await _cleanup_database(database, settings)


async def _error_handler(event: ErrorEvent) -> bool:
    exc = event.exception
    logger.error("Необработанная ошибка: %s", type(exc).__name__)

    try:
        if event.update.callback_query is not None:
            await event.update.callback_query.answer(
                "⚠️ Временная ошибка. Попробуйте ещё раз.",
                show_alert=True,
            )
        elif (
            event.update.message is not None
            and event.update.message.chat.type == "private"
        ):
            await event.update.message.answer("⚠️ Временная ошибка. Попробуйте ещё раз.")
    except TelegramForbiddenError:
        pass
    except TelegramAPIError:
        pass
    return True


async def run_bot(settings: Settings) -> int:
    database = Database(settings.database_path)
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    user_media_buffer = MediaGroupBuffer(
        delay_seconds=settings.media_group_delay_seconds,
        max_buffers=settings.media_group_max_buffers,
        max_items=settings.media_group_max_items,
    )
    owner_media_buffer = MediaGroupBuffer(
        delay_seconds=settings.media_group_delay_seconds,
        max_buffers=max(10, settings.media_group_max_buffers // 4),
        max_items=settings.media_group_max_items,
    )
    maintenance_task: asyncio.Task[None] | None = None
    stop_event = asyncio.Event()

    try:
        await database.connect()
        await database.initialize()
        await _cleanup_database(database, settings)

        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            logger.error("BOT_TOKEN отклонён Telegram. Проверьте токен в @BotFather.")
            return 1
        except TelegramAPIError as exc:
            logger.error("Не удалось подключиться к Telegram: %s", type(exc).__name__)
            return 1

        if me.id == settings.owner_id:
            logger.error("OWNER_ID не должен совпадать с ID самого бота.")
            return 1

        try:
            await bot.delete_webhook(drop_pending_updates=False)
        except TelegramAPIError as exc:
            logger.warning("Не удалось удалить webhook: %s", type(exc).__name__)

        await _setup_bot_commands(bot, settings.owner_id)

        relay = RelayService(bot, database, settings)
        rate_limiter = RateLimiter(
            window_seconds=settings.rate_limit_window_seconds,
            max_messages=settings.rate_limit_max_messages,
            min_interval_seconds=settings.rate_limit_min_interval_seconds,
            max_tracked_users=settings.rate_limit_max_tracked_users,
            warning_cooldown_seconds=settings.rate_limit_warning_cooldown_seconds,
        )
        notice_throttle = NoticeThrottle(
            cooldown_seconds=settings.rate_limit_warning_cooldown_seconds,
            max_users=settings.rate_limit_max_tracked_users,
        )

        dispatcher = Dispatcher(storage=MemoryStorage())
        dispatcher.update.outer_middleware(UpdateDeduperMiddleware(database))
        dispatcher.include_router(
            create_admin_router(
                bot=bot,
                settings=settings,
                database=database,
                relay=relay,
                media_buffer=owner_media_buffer,
            )
        )
        dispatcher.include_router(
            create_user_router(
                settings=settings,
                database=database,
                relay=relay,
                rate_limiter=rate_limiter,
                notice_throttle=notice_throttle,
                media_buffer=user_media_buffer,
            )
        )
        dispatcher.errors.register(_error_handler)

        maintenance_task = asyncio.create_task(
            _maintenance_loop(database, settings, stop_event),
            name="maintenance",
        )
        logger.info("Бот @%s запущен в режиме long polling", me.username or "bot")
        logger.info(
            "SQLite WAL включён; история хранится %s дн.",
            settings.history_retention_days,
        )

        try:
            await dispatcher.start_polling(
                bot,
                allowed_updates=dispatcher.resolve_used_update_types(),
                close_bot_session=False,
                handle_signals=True,
            )
        except TelegramConflictError:
            logger.error(
                "Конфликт long polling: запущен другой экземпляр бота "
                "или у бота остался webhook. Остановите второй процесс."
            )
            return 1
        except TelegramAPIError as exc:
            logger.error("Ошибка Telegram при polling: %s", type(exc).__name__)
            return 1
        finally:
            stop_event.set()
            if maintenance_task is not None:
                maintenance_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await maintenance_task
            await owner_media_buffer.close(flush=True)
            await user_media_buffer.close(flush=True)
        return 0
    except DatabaseError:
        logger.error("Не удалось открыть или инициализировать базу данных.")
        return 1
    finally:
        with contextlib.suppress(Exception):
            await bot.session.close()
        with contextlib.suppress(Exception):
            await database.close()


def main() -> int:
    _configure_logging()
    try:
        settings = load_settings()
    except ConfigError as exc:
        logger.error("%s", exc)
        return 1

    _configure_logging(settings.log_level)
    try:
        return asyncio.run(run_bot(settings))
    except KeyboardInterrupt:
        logger.info("Остановка по Ctrl+C")
        return 0
    except Exception as exc:  # noqa: BLE001 - верхнеуровневая защита процесса
        logger.error("Критическая ошибка запуска: %s", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
