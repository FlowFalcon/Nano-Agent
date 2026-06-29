"""
Native streaming message buffer for Telegram using sendMessageDraft.

Drafts are temporary previews. Final responses are always sent with
sendMessage and split into Telegram-sized chunks, so long answers are not
silently truncated.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Final

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods.send_message_draft import SendMessageDraft
from aiogram.types import Message

from telegram.formatters import (
    TELEGRAM_SAFE_TEXT_LENGTH,
    render_markdown_to_html,
    split_text_for_telegram,
    tail_text_for_telegram,
    telegram_html_to_plain_text,
)

logger: Final[logging.Logger] = logging.getLogger(__name__)


class MessageBuffer:
    """Buffer agent output and stream Telegram drafts when supported."""

    __slots__ = (
        "_bot", "_chat_id", "_draft_id", "_delay", "_buffer",
        "_last_edit_time", "_pending_task", "_lock", "_closed",
        "_final_message", "_enable_draft", "_send_kwargs", "_draft_kwargs",
    )

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        delay: float = 1.0,
        *,
        enable_draft: bool = True,
        send_kwargs: dict[str, Any] | None = None,
        draft_kwargs: dict[str, Any] | None = None,
    ) -> None:
        if delay < 0.5:
            raise ValueError("delay must be >= 0.5")

        self._bot = bot
        self._chat_id = chat_id
        self._draft_id = random.randint(1_000_000, 9_999_999)
        self._delay = delay
        self._buffer = ""
        self._last_edit_time = 0.0
        self._pending_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._final_message: Message | None = None
        self._enable_draft = enable_draft
        self._send_kwargs = dict(send_kwargs or {})
        self._draft_kwargs = dict(draft_kwargs or {})

    @property
    def message(self) -> Message | None:
        return self._final_message

    async def append(self, text: str) -> None:
        """Append raw Markdown/text from the agent to the buffer."""
        if self._closed:
            raise RuntimeError("Cannot append to a flushed MessageBuffer")
        if not text:
            return

        async with self._lock:
            self._buffer += text
            now = time.monotonic()
            if now - self._last_edit_time >= self._delay:
                await self._do_edit()
            elif self._pending_task is None or self._pending_task.done():
                remaining = self._delay - (now - self._last_edit_time)
                self._pending_task = asyncio.create_task(self._deferred_edit(max(remaining, 0.0)))

    async def append_blockquote(self, tool: str, target: str) -> None:
        from telegram.formatters import format_action_blockquote

        await self.append(format_action_blockquote(tool, target))

    async def append_raw(self, raw_text: str) -> None:
        await self.append(raw_text)

    async def flush(self) -> None:
        """Send the persistent final message(s)."""
        if self._closed:
            return

        if self._pending_task is not None and not self._pending_task.done():
            self._pending_task.cancel()
            try:
                await self._pending_task
            except asyncio.CancelledError:
                pass

        async with self._lock:
            self._closed = True
            if not self._buffer.strip():
                return

            source_parts = split_text_for_telegram(
                self._buffer,
                max_length=TELEGRAM_SAFE_TEXT_LENGTH,
            )

            for source_part in source_parts:
                html_content = render_markdown_to_html(source_part)
                if not html_content:
                    continue

                try:
                    self._final_message = await self._bot.send_message(
                        chat_id=self._chat_id,
                        text=html_content,
                        parse_mode=ParseMode.HTML,
                        **self._send_kwargs,
                    )
                except TelegramBadRequest as exc:
                    logger.warning("HTML final send failed; retrying as plain text: %s", exc)
                    plain = telegram_html_to_plain_text(html_content) or source_part
                    for plain_part in split_text_for_telegram(
                        plain,
                        max_length=TELEGRAM_SAFE_TEXT_LENGTH,
                    ):
                        self._final_message = await self._bot.send_message(
                            chat_id=self._chat_id,
                            text=plain_part,
                            parse_mode=None,
                            **self._send_kwargs,
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.error("Failed to send final message: %s", exc)

    async def _deferred_edit(self, wait: float) -> None:
        await asyncio.sleep(wait)
        async with self._lock:
            if not self._closed:
                await self._do_edit()

    async def _do_edit(self) -> None:
        """Send or update the temporary streaming draft."""
        if not self._enable_draft or not self._buffer:
            return

        preview_source = tail_text_for_telegram(
            self._buffer,
            max_length=TELEGRAM_SAFE_TEXT_LENGTH,
        )
        html_content = render_markdown_to_html(preview_source)

        try:
            await self._bot(
                SendMessageDraft(
                    chat_id=self._chat_id,
                    draft_id=self._draft_id,
                    text=html_content,
                    parse_mode=ParseMode.HTML,
                    **self._draft_kwargs,
                )
            )
            self._last_edit_time = time.monotonic()
        except TelegramBadRequest as exc:
            logger.debug("Draft HTML update failed; retrying plain text: %s", exc)
            try:
                plain = telegram_html_to_plain_text(html_content) or preview_source
                await self._bot(
                    SendMessageDraft(
                        chat_id=self._chat_id,
                        draft_id=self._draft_id,
                        text=tail_text_for_telegram(plain, max_length=TELEGRAM_SAFE_TEXT_LENGTH),
                        parse_mode=None,
                        **self._draft_kwargs,
                    )
                )
                self._last_edit_time = time.monotonic()
            except TelegramBadRequest as retry_exc:
                logger.debug("Draft update benign error: %s", retry_exc)
            except Exception as retry_exc:  # noqa: BLE001
                logger.error("Unexpected error during plain SendMessageDraft: %s", retry_exc)
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected error during SendMessageDraft: %s", exc)
