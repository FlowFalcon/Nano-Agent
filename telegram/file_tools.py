"""Owner-only Telegram file/artifact sending tool.

Security model:
- This tool is registered only on the normal owner/allowed-user message path.
- It is never registered for Telegram Guest Bots.
- The tool is marked ``destructive=True`` so the existing Telegram HITL approval
  keyboard must approve the exact file path before anything is uploaded.
- No directory allowlist is enforced here by design: the user's explicit approval
  is the gate. Basic file checks, directory rejection, symlink resolution, and the
  Telegram Bot API sendDocument size cap still apply.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Any, Final

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile

from core.tools import BaseTool, ToolRegistry
from telegram.formatters import render_markdown_to_html, telegram_html_to_plain_text, truncate_for_telegram

logger: Final[logging.Logger] = logging.getLogger(__name__)

_TELEGRAM_CAPTION_LIMIT: Final[int] = 1024
_TELEGRAM_BOT_API_DOCUMENT_MAX_MB: Final[int] = 50


def _resolve_file_path(file_path: str) -> Path:
    """Resolve a user/agent supplied file path to an absolute file path."""
    return Path(file_path).expanduser().resolve()


def _safe_caption(caption: str) -> tuple[str, ParseMode | None]:
    """Render a Telegram-safe caption, falling back to plain text if needed."""
    caption = (caption or "").strip()
    if not caption:
        return "", None
    html = truncate_for_telegram(render_markdown_to_html(caption), _TELEGRAM_CAPTION_LIMIT)
    if html:
        return html, ParseMode.HTML
    return truncate_for_telegram(telegram_html_to_plain_text(caption), _TELEGRAM_CAPTION_LIMIT), None


class SendTelegramFileTool(BaseTool):
    """Send an existing local file to the current Telegram owner chat after approval."""

    name = "send_telegram_file"
    description = (
        "Send an existing local file to the owner on Telegram. Use this only after "
        "the user asks to receive/export a file. This tool always requires explicit "
        "Telegram approval before upload. You may provide an absolute path or a path "
        "relative to the current working directory. Do not send secrets, tokens, private "
        "keys, config files, or credentials unless the user explicitly asks for that exact file."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute or relative path to the local file to send.",
            },
            "caption": {
                "type": "string",
                "description": "Optional short caption to show with the file. Markdown is allowed.",
                "default": "",
            },
            "send_as_photo": {
                "type": "boolean",
                "description": (
                    "If true and the file is an image, send it as a Telegram photo preview. "
                    "Default false sends it as a document to preserve the exact file."
                ),
                "default": False,
            },
        },
        "required": ["file_path"],
    }
    destructive = True

    def __init__(
        self,
        *,
        bot: Bot,
        chat_id: int,
        max_file_size_mb: int = _TELEGRAM_BOT_API_DOCUMENT_MAX_MB,
        send_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._max_bytes = min(max_file_size_mb, _TELEGRAM_BOT_API_DOCUMENT_MAX_MB) * 1024 * 1024
        self._send_kwargs = dict(send_kwargs or {})

    async def execute(self, **params: Any) -> str:
        file_path = str(params.get("file_path", "")).strip()
        caption_raw = str(params.get("caption", "") or "")
        send_as_photo = bool(params.get("send_as_photo", False))

        if not file_path:
            return "Error: file_path is required."

        resolved = _resolve_file_path(file_path)

        if not resolved.exists():
            return f"Error: file does not exist: {resolved}"
        if not resolved.is_file():
            return f"Error: path is not a regular file: {resolved}"

        size = resolved.stat().st_size
        if size <= 0:
            return f"Error: file is empty: {resolved}"
        if size > self._max_bytes:
            return (
                f"Error: file is {size / (1024 * 1024):.1f} MB, "
                f"which exceeds max_send_file_size_mb={self._max_bytes / (1024 * 1024):.0f}."
            )

        caption, parse_mode = _safe_caption(caption_raw)
        mime_type, _ = mimetypes.guess_type(str(resolved))
        is_image = bool(mime_type and mime_type.startswith("image/"))
        input_file = FSInputFile(str(resolved), filename=resolved.name)

        try:
            if send_as_photo and is_image:
                await self._bot.send_photo(
                    chat_id=self._chat_id,
                    photo=input_file,
                    caption=caption or None,
                    parse_mode=parse_mode,
                    **self._send_kwargs,
                )
                mode = "photo"
            else:
                await self._bot.send_document(
                    chat_id=self._chat_id,
                    document=input_file,
                    caption=caption or None,
                    parse_mode=parse_mode,
                    **self._send_kwargs,
                )
                mode = "document"
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to send Telegram file %s", resolved)
            return f"Error sending file to Telegram: {type(exc).__name__}: {exc}"

        return f"Sent {mode}: {resolved} ({size} bytes)."


def attach_telegram_file_tool(
    tools: ToolRegistry | None,
    *,
    bot: Bot,
    chat_id: int,
    max_file_size_mb: int = _TELEGRAM_BOT_API_DOCUMENT_MAX_MB,
    send_kwargs: dict[str, Any] | None = None,
) -> ToolRegistry | None:
    """Return a chat-bound copy of ``tools`` with send_telegram_file attached."""
    if tools is None:
        return None
    registry = tools.clone()
    registry.register(
        SendTelegramFileTool(
            bot=bot,
            chat_id=chat_id,
            max_file_size_mb=max_file_size_mb,
            send_kwargs=send_kwargs,
        )
    )
    return registry
