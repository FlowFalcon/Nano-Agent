"""Helpers for keeping Telegram replies inside the current topic/thread."""

from __future__ import annotations

from typing import Any

from aiogram.types import Message


def _as_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def message_thread_id(message: Message) -> int | None:
    """Return the Bot API message_thread_id when the inbound message has one."""
    return _as_int_or_none(getattr(message, "message_thread_id", None))


def direct_messages_topic_id(message: Message) -> int | None:
    """Return direct_messages_topic_id for channel direct-message topics, if present."""
    topic_id = getattr(message, "direct_messages_topic_id", None)
    direct_topic = getattr(message, "direct_messages_topic", None)
    if topic_id is None and direct_topic is not None:
        topic_id = getattr(direct_topic, "topic_id", None) or getattr(direct_topic, "id", None)
    return _as_int_or_none(topic_id)


def topic_send_kwargs(message: Message) -> dict[str, int]:
    """Keyword args for Bot API send* methods targeting the same Telegram topic.

    Private bot topics and forum supergroup topics use ``message_thread_id``.
    Channel direct-message topics use ``direct_messages_topic_id``. They are
    intentionally not sent together because Telegram treats them as different
    routing mechanisms.
    """
    thread_id = message_thread_id(message)
    if thread_id is not None:
        return {"message_thread_id": thread_id}

    direct_topic_id = direct_messages_topic_id(message)
    if direct_topic_id is not None:
        return {"direct_messages_topic_id": direct_topic_id}

    return {}


def draft_topic_kwargs(message: Message) -> dict[str, int]:
    """Keyword args for sendMessageDraft targeting the same private topic."""
    thread_id = message_thread_id(message)
    if thread_id is not None:
        return {"message_thread_id": thread_id}
    return {}
