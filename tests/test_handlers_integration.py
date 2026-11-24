from __future__ import annotations

import time
from typing import Any

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update

from database import Database
from handlers.admin import create_admin_router
from handlers.user import create_user_router
from services.media_groups import MediaGroupBuffer
from services.middleware import UpdateDeduperMiddleware
from services.rate_limit import NoticeThrottle, RateLimiter
from services.relay import RelayService
from tests.conftest import make_settings

OWNER_ID = 777


class FakeBot:
    id = 999_000

    def __init__(self) -> None:
        self.direct_calls: list[tuple[str, dict[str, Any]]] = []
        self.shortcut_calls: list[str] = []
        self.shortcut_texts: list[str] = []
        self.shortcut_messages: list[tuple[str, Any]] = []
        self._next_id = 100

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def __call__(self, method: Any) -> Any:
        self.shortcut_calls.append(type(method).__name__)
        text = getattr(method, "text", None)
        if isinstance(text, str):
            self.shortcut_texts.append(text)
            self.shortcut_messages.append((text, getattr(method, "reply_markup", None)))
        if type(method).__name__ == "AnswerCallbackQuery":
            return True
        return type("Sent", (), {"message_id": self._id()})()

    async def send_message(self, **kwargs: Any) -> Any:
        self.direct_calls.append(("send_message", kwargs))
        return type("Sent", (), {"message_id": self._id()})()

    async def copy_message(self, **kwargs: Any) -> Any:
        self.direct_calls.append(("copy_message", kwargs))
        return type("Sent", (), {"message_id": self._id()})()

    async def copy_messages(self, **kwargs: Any) -> list[Any]:
        self.direct_calls.append(("copy_messages", kwargs))
        return [
            type("Sent", (), {"message_id": self._id()})()
            for _ in kwargs["message_ids"]
        ]

    async def edit_message_text(self, **kwargs: Any) -> Any:
        self.direct_calls.append(("edit_message_text", kwargs))
        return type("Sent", (), {"message_id": kwargs["message_id"]})()


def user_dict(
    user_id: int, *, text: str, message_id: int, username: str | None = "user"
):
    return {
        "id": user_id,
        "is_bot": False,
        "first_name": "Пользователь",
        "username": username,
    }


def private_chat(user_id: int) -> dict[str, Any]:
    return {"id": user_id, "type": "private"}


def make_text_update(
    update_id: int,
    *,
    user_id: int,
    text: str,
    message_id: int,
    reply_to_message_id: int | None = None,
    username: str | None = "user",
) -> Update:
    payload: dict[str, Any] = {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "date": int(time.time()),
            "chat": private_chat(user_id),
            "from": user_dict(
                user_id, text=text, message_id=message_id, username=username
            ),
            "text": text,
        },
    }
    if reply_to_message_id is not None:
        payload["message"]["reply_to_message"] = {
            "message_id": reply_to_message_id,
            "date": int(time.time()),
            "chat": private_chat(OWNER_ID),
            "from": {"id": 999_000, "is_bot": True, "first_name": "Bot"},
            "text": "Карточка",
        }
    return Update.model_validate(payload)


def make_callback_update(
    update_id: int,
    *,
    from_user_id: int,
    data: str,
    message_id: int = 500,
) -> Update:
    return Update.model_validate(
        {
            "update_id": update_id,
            "callback_query": {
                "id": f"cb-{update_id}",
                "from": user_dict(from_user_id, text="", message_id=message_id),
                "chat_instance": "test",
                "data": data,
                "message": {
                    "message_id": message_id,
                    "date": int(time.time()),
                    "chat": private_chat(from_user_id),
                    "from": {"id": 999_000, "is_bot": True, "first_name": "Bot"},
                    "text": "Карточка",
                },
            },
        }
    )


async def build_dispatcher(tmp_path):
    db = Database(tmp_path / "integration.db")
    await db.connect()
    await db.initialize()
    settings = make_settings(tmp_path)
    bot = FakeBot()
    buffers = MediaGroupBuffer(delay_seconds=0.01, max_buffers=10, max_items=10)
    relay = RelayService(bot, db, settings)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.update.outer_middleware(UpdateDeduperMiddleware(db))
    dispatcher.include_router(
        create_admin_router(
            bot=bot,
            settings=settings,
            database=db,
            relay=relay,
            media_buffer=buffers,
        )
    )
    dispatcher.include_router(
        create_user_router(
            settings=settings,
            database=db,
            relay=relay,
            rate_limiter=RateLimiter(
                window_seconds=60,
                max_messages=30,
                min_interval_seconds=0,
                max_tracked_users=100,
                warning_cooldown_seconds=30,
            ),
            notice_throttle=NoticeThrottle(),
            media_buffer=buffers,
        )
    )
    return db, bot, dispatcher, buffers


async def test_user_message_then_owner_reply_via_telegram_reply(tmp_path) -> None:
    db, bot, dispatcher, buffers = await build_dispatcher(tmp_path)
    try:
        await dispatcher.feed_update(
            bot,
            make_text_update(
                1, user_id=1001, text="Сообщение пользователя", message_id=10
            ),
        )
        sends = [call for call in bot.direct_calls if call[0] == "send_message"]
        assert len(sends) == 1
        assert sends[0][1]["chat_id"] == OWNER_ID
        assert "Сообщение пользователя" in sends[0][1]["text"]
        ack_text, ack_keyboard = next(
            (text, markup)
            for text, markup in bot.shortcut_messages
            if text.startswith("✅ Ваше сообщение доставлено владельцу.")
        )
        assert "Хотите отправить ещё одно сообщение?" in ack_text
        assert ack_keyboard.inline_keyboard[0][0].text == "✍️ Написать ещё"
        assert "1001" not in sends[0][1]["text"]
        assert "@user" not in sends[0][1]["text"]
        assert "Пользователь" not in sends[0][1]["text"]

        card_id = 101
        await dispatcher.feed_update(
            bot,
            make_text_update(
                2,
                user_id=OWNER_ID,
                text="Ответ владельца",
                message_id=20,
                reply_to_message_id=card_id,
                username="owner_private_name",
            ),
        )
        copies = [call for call in bot.direct_calls if call[0] == "copy_message"]
        assert len(copies) == 1
        assert copies[0][1]["chat_id"] == 1001
        assert copies[0][1]["from_chat_id"] == OWNER_ID
        assert copies[0][1]["message_id"] == 20
    finally:
        await buffers.close(flush=False)
        await db.close()


async def test_callback_and_fsm_reply(tmp_path) -> None:
    db, bot, dispatcher, buffers = await build_dispatcher(tmp_path)
    try:
        await dispatcher.feed_update(
            bot,
            make_text_update(10, user_id=2001, text="Первое", message_id=30),
        )

        await dispatcher.feed_update(
            bot,
            make_callback_update(
                11, from_user_id=OWNER_ID, data="dlg:reply:1", message_id=501
            ),
        )
        prompt_calls = [call for call in bot.shortcut_calls if call == "SendMessage"]
        assert prompt_calls

        await dispatcher.feed_update(
            bot,
            make_text_update(
                12, user_id=OWNER_ID, text="Ответ через FSM", message_id=21
            ),
        )
        copies = [call for call in bot.direct_calls if call[0] == "copy_message"]
        assert copies[-1][1]["chat_id"] == 2001

        await dispatcher.feed_update(
            bot,
            make_callback_update(
                13, from_user_id=OWNER_ID, data="dlg:profile:1", message_id=501
            ),
        )
        edits = [call for call in bot.direct_calls if call[0] == "edit_message_text"]
        assert edits
        assert "ДИАЛОГ #1" in edits[-1][1]["text"]
        assert "2001" not in edits[-1][1]["text"]
        assert "@user" not in edits[-1][1]["text"]
    finally:
        await buffers.close(flush=False)
        await db.close()


async def test_commands_are_case_insensitive(tmp_path) -> None:
    db, bot, dispatcher, buffers = await build_dispatcher(tmp_path)
    try:
        before = bot.shortcut_calls.count("SendMessage")
        await dispatcher.feed_update(
            bot,
            make_text_update(30, user_id=4001, text="/Start", message_id=40),
        )
        after_user = bot.shortcut_calls.count("SendMessage")
        assert after_user == before + 1

        await dispatcher.feed_update(
            bot,
            make_text_update(31, user_id=OWNER_ID, text="/MeNu", message_id=41),
        )
        after_owner = bot.shortcut_calls.count("SendMessage")
        assert after_owner == after_user + 1
    finally:
        await buffers.close(flush=False)
        await db.close()


async def test_non_owner_admin_callback_is_ignored(tmp_path) -> None:
    db, bot, dispatcher, buffers = await build_dispatcher(tmp_path)
    try:
        await dispatcher.feed_update(
            bot,
            make_callback_update(
                20, from_user_id=3001, data="menu:home", message_id=600
            ),
        )
        assert not [call for call in bot.direct_calls if call[0] == "edit_message_text"]
    finally:
        await buffers.close(flush=False)
        await db.close()
