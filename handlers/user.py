"""Обработчики обычных пользователей."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

import keyboards
import ui
from callbacks import UserCB
from config import Settings
from database import Database, DatabaseError
from services.content import extract_single
from services.media_groups import MediaGroupBuffer
from services.rate_limit import NoticeThrottle, RateLimiter
from services.relay import RelayService
from services.telegram import safe_telegram_call

logger = logging.getLogger(__name__)


def create_user_router(
    *,
    settings: Settings,
    database: Database,
    relay: RelayService,
    rate_limiter: RateLimiter,
    notice_throttle: NoticeThrottle,
    media_buffer: MediaGroupBuffer,
) -> Router:
    router = Router(name="user")
    router.message.filter(
        F.chat.type == "private",
        F.from_user.id != settings.owner_id,
    )
    router.callback_query.filter(
        F.message.chat.type == "private",
        F.from_user.id != settings.owner_id,
    )

    async def send_text(
        message: Message,
        text: str,
        *,
        reply_markup=None,
    ) -> None:
        try:
            await safe_telegram_call(
                lambda: message.answer(text, reply_markup=reply_markup),
                operation="user_message_answer",
                retries=settings.telegram_max_retries,
                max_retry_after=settings.floodwait_max_seconds,
            )
        except TelegramAPIError as exc:
            logger.warning("Ответ пользователю не доставлен: %s", type(exc).__name__)

    @router.message(CommandStart())
    async def handle_start(message: Message) -> None:
        if message.from_user is None:
            return
        try:
            dialog = await database.get_or_create_dialog(
                message.from_user.id,
                message.from_user.full_name,
                message.from_user.username,
            )
        except DatabaseError:
            await send_text(message, ui.delivery_failed_user_text())
            return

        if dialog.is_blocked:
            await send_text(message, ui.blocked_user_text())
            return
        await send_text(
            message,
            ui.welcome_text(),
            reply_markup=keyboards.user_start_keyboard(),
        )

    @router.message(Command("help"))
    async def handle_help(message: Message) -> None:
        await send_text(
            message,
            ui.user_help_text(),
            reply_markup=keyboards.user_start_keyboard(),
        )

    @router.message(Command("cancel"))
    async def handle_cancel(message: Message) -> None:
        await send_text(
            message,
            "ℹ️ У тебя нет активных действий. Просто отправь сообщение.",
        )

    @router.callback_query(UserCB.filter(F.action == "compose"))
    async def handle_compose(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message is not None:
            await send_text(
                callback.message,
                "✍️ Напиши сообщение прямо в этот чат — текст или вложение.",
            )

    @router.callback_query(UserCB.filter(F.action == "help"))
    async def handle_callback_help(callback: CallbackQuery) -> None:
        await callback.answer()
        if callback.message is not None:
            await send_text(
                callback.message,
                ui.user_help_text(),
                reply_markup=keyboards.user_start_keyboard(),
            )

    async def flush_album(messages: Sequence[Message]) -> None:
        try:
            delivered = await relay.deliver_user_album(messages)
        except DatabaseError:
            logger.error("Не удалось сохранить альбом пользователя")
            delivered = False
        if not delivered and messages:
            await send_text(messages[0], ui.delivery_failed_user_text())

    @router.message()
    async def handle_message(message: Message) -> None:
        if message.from_user is None:
            return

        try:
            dialog = await database.get_or_create_dialog(
                message.from_user.id,
                message.from_user.full_name,
                message.from_user.username,
            )
        except DatabaseError:
            await send_text(message, ui.delivery_failed_user_text())
            return

        if dialog.is_blocked:
            if notice_throttle.allow(message.from_user.id):
                await send_text(message, ui.blocked_user_text())
            return

        content = extract_single(message)
        if content is None:
            await send_text(message, ui.unsupported_user_text())
            return

        is_album = bool(message.media_group_id)
        decision = rate_limiter.allow(message.from_user.id, is_album=is_album)
        if not decision.allowed:
            if rate_limiter.warning_due(message.from_user.id):
                await send_text(message, ui.rate_limited_text(decision.retry_after))
            return

        if is_album:
            try:
                await media_buffer.add(message, flush_album)
            except Exception as exc:  # noqa: BLE001 - буфер не должен ронять polling
                logger.error("Ошибка буфера альбома: %s", type(exc).__name__)
                await send_text(message, ui.delivery_failed_user_text())
            return

        try:
            delivered = await relay.deliver_user_message(message, dialog, content)
        except DatabaseError:
            logger.error("Не удалось сохранить сообщение пользователя")
            delivered = False
        if not delivered:
            await send_text(message, ui.delivery_failed_user_text())

    return router
