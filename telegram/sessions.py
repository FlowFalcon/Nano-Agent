"""Persistent Telegram topic-based chat sessions."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from aiogram.types import Message

logger: Final[logging.Logger] = logging.getLogger(__name__)

MAX_HISTORY_TURNS: Final[int] = 50


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _safe_topic_key(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "default"


def resolve_topic_key(message: Message) -> str:
    """Build a stable session key from Telegram chat/topic metadata."""
    chat_id = getattr(message.chat, "id", None) or "unknown"
    thread_id = getattr(message, "message_thread_id", None)

    direct_topic_id = getattr(message, "direct_messages_topic_id", None)
    direct_topic = getattr(message, "direct_messages_topic", None)
    if direct_topic_id is None and direct_topic is not None:
        direct_topic_id = getattr(direct_topic, "topic_id", None) or getattr(direct_topic, "id", None)

    if thread_id is not None:
        raw = f"chat_{chat_id}_thread_{thread_id}"
    elif direct_topic_id is not None:
        raw = f"chat_{chat_id}_direct_{direct_topic_id}"
    else:
        raw = f"chat_{chat_id}_main"

    return _safe_topic_key(raw)


class TopicSessionStore:
    """Store one clean JSON history array per Telegram topic."""

    def __init__(self, workspace_dir: str, *, max_turns: int = MAX_HISTORY_TURNS) -> None:
        self.sessions_dir = Path(workspace_dir) / "sessions"
        self.metadata_path = self.sessions_dir / "metadata.json"
        self.legacy_path = Path(workspace_dir) / ".sessions" / "chat_histories.json"
        self.max_turns = max_turns
        self._histories: dict[str, list[dict[str, str]]] = {}
        self._metadata: dict[str, dict[str, Any]] = {}
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return

        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        if self.metadata_path.exists():
            try:
                raw_metadata = json.loads(self.metadata_path.read_text("utf-8"))
                if isinstance(raw_metadata, dict):
                    self._metadata = raw_metadata
            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to load session metadata: %s", exc)

        self._migrate_legacy_history()
        self._loaded = True

    def topic_key(self, message: Message) -> str:
        self.load()
        key = resolve_topic_key(message)
        self._touch_metadata(key, message)
        return key

    def get_history(self, message: Message) -> list[dict[str, str]]:
        key = self.topic_key(message)
        if key not in self._histories:
            self._histories[key] = self._read_history(key)
        return self._histories[key]

    def save_history(self, message: Message, history: list[dict[str, str]]) -> None:
        key = self.topic_key(message)
        self.trim(history)
        self._histories[key] = history
        self._history_path(key).write_text(_json_dump(history), "utf-8")
        self._save_metadata()

    def reset_history(self, message: Message) -> None:
        key = self.topic_key(message)
        self._histories[key] = []
        self._history_path(key).write_text(_json_dump([]), "utf-8")
        self._save_metadata()

    def render_history(self, message: Message, *, limit: int = 20) -> str:
        history = self.get_history(message)
        if not history:
            return "No conversation history for this topic yet."

        recent = history[-limit:]
        lines: list[str] = []
        for item in recent:
            role = item.get("role", "unknown")
            content = (item.get("content", "") or "").strip()
            if len(content) > 700:
                content = content[:700].rstrip() + "..."
            label = "User" if role == "user" else "Assistant" if role == "assistant" else role.title()
            lines.append(f"{label}: {content}")
        return "\n\n".join(lines)

    def session_count(self) -> int:
        self.load()
        return len(list(self.sessions_dir.glob("*.json"))) - (1 if self.metadata_path.exists() else 0)

    def trim(self, history: list[dict[str, str]]) -> None:
        while len(history) > self.max_turns:
            history.pop(0)

    def _history_path(self, key: str) -> Path:
        return self.sessions_dir / f"{_safe_topic_key(key)}.json"

    def _read_history(self, key: str) -> list[dict[str, str]]:
        path = self._history_path(key)
        if not path.exists():
            return []
        try:
            raw_history = json.loads(path.read_text("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load session history %s: %s", path, exc)
            return []
        if not isinstance(raw_history, list):
            return []

        cleaned: list[dict[str, str]] = []
        for item in raw_history:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = item.get("content")
            if role in {"user", "assistant", "tool", "system"} and isinstance(content, str):
                cleaned.append({"role": role, "content": content})
        self.trim(cleaned)
        return cleaned

    def _touch_metadata(self, key: str, message: Message) -> None:
        chat = message.chat
        thread_id = getattr(message, "message_thread_id", None)
        direct_topic_id = getattr(message, "direct_messages_topic_id", None)
        direct_topic = getattr(message, "direct_messages_topic", None)
        if direct_topic_id is None and direct_topic is not None:
            direct_topic_id = getattr(direct_topic, "topic_id", None) or getattr(direct_topic, "id", None)
        title = getattr(chat, "title", None) or getattr(chat, "full_name", None) or getattr(chat, "username", None)
        self._metadata[key] = {
            "chat_id": getattr(chat, "id", None),
            "chat_type": str(getattr(chat, "type", "")),
            "thread_id": thread_id,
            "direct_messages_topic_id": direct_topic_id,
            "is_topic_message": bool(getattr(message, "is_topic_message", False)),
            "has_topics_enabled": bool(getattr(chat, "has_topics_enabled", False)),
            "title": title or "main",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _save_metadata(self) -> None:
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_path.write_text(_json_dump(self._metadata), "utf-8")

    def _migrate_legacy_history(self) -> None:
        if not self.legacy_path.exists():
            return

        try:
            legacy_data = json.loads(self.legacy_path.read_text("utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to read legacy chat history: %s", exc)
            return

        if not isinstance(legacy_data, dict):
            return

        migrated = False
        for chat_id, history in legacy_data.items():
            if not isinstance(history, list):
                continue
            key = _safe_topic_key(f"chat_{chat_id}_main")
            path = self._history_path(key)
            if not path.exists():
                path.write_text(_json_dump(history), "utf-8")
                migrated = True
            self._metadata.setdefault(
                key,
                {
                    "chat_id": chat_id,
                    "chat_type": "legacy",
                    "thread_id": None,
                    "direct_messages_topic_id": None,
                    "is_topic_message": False,
                    "has_topics_enabled": False,
                    "title": "legacy main",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )

        if migrated:
            self._save_metadata()
