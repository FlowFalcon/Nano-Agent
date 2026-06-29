"""Allowlist middleware for the private owner Telegram bot.

Normal Telegram messages are handled only when the sender is in
``allowed_user_ids``. Non-allowed users do not reach the agent, tools, or
workspace memory.

Telegram Guest Bots are handled by ``router.guest_message`` separately and do
not use this middleware path.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Final

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


logger: Final[logging.Logger] = logging.getLogger(__name__)


class AllowedUsersMiddleware(BaseMiddleware):
    """Block normal Telegram events from users outside the owner allowlist."""

    __slots__ = ("_allowed_user_ids", "_unauthorized_message")

    def __init__(self, allowed_user_ids: list[int], unauthorized_message: str) -> None:
        self._allowed_user_ids = frozenset(allowed_user_ids)
        self._unauthorized_message = unauthorized_message
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id: int | None = None

        if isinstance(event, Message) and event.from_user is not None:
            user_id = event.from_user.id
        elif isinstance(event, CallbackQuery) and event.from_user is not None:
            user_id = event.from_user.id

        if user_id is None:
            logger.info("Blocked Telegram event without a concrete user sender.")
            return None

        if user_id not in self._allowed_user_ids:
            logger.info("Blocked non-allowed Telegram user %d.", user_id)
            if isinstance(event, Message):
                try:
                    await event.answer(self._unauthorized_message)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Could not send unauthorized message: %s", exc)
            elif isinstance(event, CallbackQuery):
                try:
                    await event.answer("Unauthorized", show_alert=True)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Could not answer unauthorized callback: %s", exc)
            return None

        data["is_allowed_user"] = True
        return await handler(event, data)
