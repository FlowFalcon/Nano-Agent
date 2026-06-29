"""Telegram message, command, approval, and clarify handlers."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from typing import Any, Final

from aiogram import F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.methods.create_forum_topic import CreateForumTopic
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from config.settings import AppConfig, save_config_update
from telegram.buffer import MessageBuffer
from telegram.file_tools import attach_telegram_file_tool
from telegram.formatters import (
    format_action_blockquote,
    format_error_message,
    escape_html_text,
)
from telegram.sessions import TopicSessionStore
from telegram.thread_utils import draft_topic_kwargs, topic_send_kwargs

logger: Final[logging.Logger] = logging.getLogger(__name__)
router: Final[Router] = Router(name="telegram_handlers")

_pending_approvals: dict[str, asyncio.Event] = {}
_approval_results: dict[str, bool] = {}
_active_tasks: dict[str, asyncio.Task[Any]] = {}

# Clarify (ask_user): same Event pattern as approvals, but the result is text.
_pending_clarify: dict[str, asyncio.Event] = {}
_clarify_results: dict[str, str] = {}
_clarify_options: dict[str, list[str]] = {}
_clarify_awaiting_text: dict[int, str] = {}  # chat_id -> event_id (for "Other" free text)
_CLARIFY_TIMEOUT_SECONDS: Final[int] = 300

# Session-level tool approval cache: cleared on each new user message.
# When the user taps "Allow this session", a (tool_name, args_preview) key is
# stored here so the same tool+args don't ask again for the rest of the session.
_session_approved: dict[str, str] = {}
_SESSION_KEY_SEP: Final[str] = "::"

def _session_store(config: AppConfig) -> TopicSessionStore:
    return TopicSessionStore(config.system.workspace_dir)


async def _answer_in_topic(message: Message, text: str, **kwargs: Any) -> Message:
    """Send a reply to the same Telegram topic/thread as the incoming message.

    aiogram's Message.answer() already forwards message_thread_id from the
    source message. Passing it manually as well raises:
    TypeError: SendMessage() got multiple values for keyword argument
    'message_thread_id'.
    """
    return await message.answer(text, **kwargs)

def _help_text() -> str:
    return (
        "<b>Nano-Agent Commands</b>\n\n"
        "/help - show this guide\n"
        "/new - reset this topic's session (saves what was learned first)\n"
        "/stop - stop the running task\n"
        "/restart - restart the bot process\n"
        "/status - show agent status\n\n"
        "<b>Admin (owner)</b>\n"
        "/model [model] - view/change the primary model\n"
        "/shell [ask|list|all] - view/change shell safety mode\n"
        "/allow [cmd] - view/add to the shell allowlist\n"
        "/block [cmd] - view/add to the shell blocklist\n"
        "/mcp - list MCP servers\n"
        "/skills - list active skills\n\n"
        "Admin changes are saved to config immediately. "
        "Send a normal message to talk to the agent. In groups/forums and private-chat topics, history is kept per topic. "
        "(Tip: <code>/topic &lt;name&gt;</code> opens a new topic in forums.)"
    )


def _status_text(config: AppConfig, store: TopicSessionStore, topic_key: str) -> str:
    active = "Active" if topic_key in _active_tasks and not _active_tasks[topic_key].done() else "Idle"
    return (
        "<b>Nano-Agent Status</b>\n\n"
        f"- <b>Name:</b> {escape_html_text(config.system.agent_name)}\n"
        f"- <b>Workspace:</b> <code>{escape_html_text(config.system.workspace_dir)}</code>\n"
        f"- <b>Shell exec mode:</b> <code>{escape_html_text(config.system.exec_approval_mode)}</code>\n"
        f"- <b>Draft streaming:</b> {'On' if config.telegram.enable_private_draft_streaming else 'Off'}\n"
        f"- <b>Current topic:</b> <code>{escape_html_text(topic_key)}</code>\n"
        f"- <b>Sessions:</b> {store.session_count()}\n"
        f"- <b>Task:</b> {active}"
    )


def _cancel_active_task(topic_key: str) -> bool:
    task = _active_tasks.get(topic_key)
    if task is None or task.done():
        return False
    task.cancel()
    return True


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    await _answer_in_topic(message, _help_text(), parse_mode=ParseMode.HTML)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await _answer_in_topic(message, _help_text(), parse_mode=ParseMode.HTML)


@router.message(Command("new"))
async def cmd_reset_history(message: Message, config: AppConfig, **kwargs: Any) -> None:
    store = _session_store(config)
    topic_key = store.topic_key(message)
    stopped = _cancel_active_task(topic_key)

    # Session boundary: distil durable knowledge into long-term memory before wiping
    # history. Runs in the background so /new stays instant; failures never block it.
    learned = ""
    memory = kwargs.get("memory")
    history = list(store.get_history(message))
    if memory is not None and len(history) >= 4:
        from core.memory import reflect_on_session

        asyncio.create_task(reflect_on_session(history, memory, config.llm.providers))
        learned = " I saved what I learned to memory."

    store.reset_history(message)
    suffix = " The active task was also stopped." if stopped else ""
    await _answer_in_topic(message, f"This topic's session has been reset.{learned}{suffix}")


@router.message(Command("stop"))
async def cmd_stop(message: Message, config: AppConfig) -> None:
    store = _session_store(config)
    topic_key = store.topic_key(message)
    if _cancel_active_task(topic_key):
        await _answer_in_topic(message, "Stopping the active task.")
    else:
        await _answer_in_topic(message, "No active task in this topic.")


@router.message(Command("restart"))
async def cmd_restart(message: Message) -> None:
    await _answer_in_topic(message, "Restarting the bot now.")

    async def restart_later() -> None:
        await asyncio.sleep(0.5)
        os.execv(sys.executable, [sys.executable, *sys.argv])

    asyncio.create_task(restart_later())


@router.message(Command("topic"))
async def cmd_create_topic(message: Message) -> None:
    """Create a real Telegram topic, then send a starter message into it."""
    if message.text is None:
        return

    raw_name = message.text.split(maxsplit=1)[1].strip() if len(message.text.split(maxsplit=1)) > 1 else ""
    topic_name = raw_name or "Nano-Agent"
    if len(topic_name) > 128:
        topic_name = topic_name[:128].rstrip()

    try:
        topic = await message.bot(
            CreateForumTopic(
                chat_id=message.chat.id,
                name=topic_name,
            )
        )
        thread_id = getattr(topic, "message_thread_id", None)
        if thread_id is None:
            await _answer_in_topic(message, "Topic created, but Telegram did not return a message_thread_id.")
            return

        await message.bot.send_message(
            chat_id=message.chat.id,
            message_thread_id=thread_id,
            text=f"Topic '{escape_html_text(topic_name)}' is ready.",
            parse_mode=ParseMode.HTML,
        )
    except TelegramBadRequest as exc:
        await _answer_in_topic(
            message,
            "Could not create the topic. Make sure topic/forum mode is enabled in @BotFather for private chat. "
            "If this is a supergroup, the bot must be an admin with can_manage_topics.\n\n"
            f"<code>{escape_html_text(str(exc))}</code>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to create Telegram topic: %s", exc)
        await _answer_in_topic(
            message,
            f"Could not create topic: <code>{escape_html_text(type(exc).__name__)}: {escape_html_text(str(exc))}</code>",
            parse_mode=ParseMode.HTML,
        )

@router.message(Command("status"))
async def cmd_status(message: Message, config: AppConfig) -> None:
    store = _session_store(config)
    topic_key = store.topic_key(message)
    await _answer_in_topic(message, _status_text(config, store, topic_key), parse_mode=ParseMode.HTML)


def _command_arg(message: Message) -> str:
    """Return the text after a /command, or empty string."""
    if not message.text:
        return ""
    parts = message.text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


async def _save_and_reply(message: Message, updater: Any, ok_text: str) -> None:
    """Persist a config change via save_config_update and report success/failure."""
    try:
        save_config_update(updater)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Config update failed: %s", exc)
        await _answer_in_topic(message, f"Failed to save: <code>{escape_html_text(str(exc))}</code>", parse_mode=ParseMode.HTML)
        return
    await _answer_in_topic(message, ok_text, parse_mode=ParseMode.HTML)


@router.message(Command("model"))
async def cmd_model(message: Message, config: AppConfig) -> None:
    arg = _command_arg(message)
    if not arg:
        lines = [f"{i + 1}. {escape_html_text(p.name)} → <code>{escape_html_text(p.model)}</code>" for i, p in enumerate(config.llm.providers)]
        await _answer_in_topic(message, "<b>Providers &amp; models</b>\n" + "\n".join(lines) + "\n\nChange primary model: <code>/model &lt;model-name&gt;</code>", parse_mode=ParseMode.HTML)
        return

    def updater(raw: dict[str, Any]) -> None:
        provs = raw.get("llm", {}).get("providers", [])
        if provs:
            provs[0]["model"] = arg

    await _save_and_reply(message, updater, f"✅ Primary model → <code>{escape_html_text(arg)}</code> (saved).")
    if config.llm.providers:
        config.llm.providers[0].model = arg


@router.message(Command("shell"))
async def cmd_shell(message: Message, config: AppConfig) -> None:
    arg = _command_arg(message).lower()
    valid = {"ask": "approval", "approval": "approval", "list": "list", "all": "all"}
    if arg not in valid:
        await _answer_in_topic(message, f"Current shell mode: <code>{escape_html_text(config.system.exec_approval_mode)}</code>\nChange: <code>/shell ask|list|all</code>", parse_mode=ParseMode.HTML)
        return
    mode = valid[arg]

    def updater(raw: dict[str, Any]) -> None:
        raw.setdefault("system", {})["exec_approval_mode"] = mode

    await _save_and_reply(message, updater, f"✅ Shell mode → <code>{mode}</code> (saved).")
    config.system.exec_approval_mode = mode  # type: ignore[assignment]


@router.message(Command("allow"))
async def cmd_allow(message: Message, config: AppConfig) -> None:
    arg = _command_arg(message)
    if not arg:
        cur = config.system.exec_allowed_commands
        body = "\n".join(f"- <code>{escape_html_text(c)}</code>" for c in cur) or "(empty)"
        await _answer_in_topic(message, f"<b>Shell allowlist:</b>\n{body}\n\nAdd: <code>/allow &lt;command&gt;</code>", parse_mode=ParseMode.HTML)
        return

    def updater(raw: dict[str, Any]) -> None:
        lst = raw.setdefault("system", {}).setdefault("exec_allowed_commands", [])
        if arg not in lst:
            lst.append(arg)

    await _save_and_reply(message, updater, f"✅ Allowlist + <code>{escape_html_text(arg)}</code> (saved).")
    if arg not in config.system.exec_allowed_commands:
        config.system.exec_allowed_commands.append(arg)


@router.message(Command("block"))
async def cmd_block(message: Message, config: AppConfig) -> None:
    arg = _command_arg(message)
    if not arg:
        cur = config.system.exec_blocked_commands
        body = "\n".join(f"- <code>{escape_html_text(c)}</code>" for c in cur) or "(empty)"
        await _answer_in_topic(message, f"<b>Shell blocklist:</b>\n{body}\n\nAdd: <code>/block &lt;command&gt;</code>", parse_mode=ParseMode.HTML)
        return

    def updater(raw: dict[str, Any]) -> None:
        lst = raw.setdefault("system", {}).setdefault("exec_blocked_commands", [])
        if arg not in lst:
            lst.append(arg)

    await _save_and_reply(message, updater, f"✅ Blocklist + <code>{escape_html_text(arg)}</code> (saved).")
    if arg not in config.system.exec_blocked_commands:
        config.system.exec_blocked_commands.append(arg)


@router.message(Command("mcp"))
async def cmd_mcp(message: Message, config: AppConfig) -> None:
    servers = config.mcp.servers
    if not servers:
        await _answer_in_topic(message, "No MCP servers configured.")
        return
    lines = [f"- <b>{escape_html_text(s.name)}</b> ({escape_html_text(s.transport)})" for s in servers]
    await _answer_in_topic(message, "<b>MCP servers:</b>\n" + "\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("skills"))
async def cmd_skills(message: Message, config: AppConfig) -> None:
    from pathlib import Path

    from core.skills import load_skills_dir

    skills = load_skills_dir(Path(config.system.workspace_dir) / "skills")
    if not skills:
        await _answer_in_topic(message, "No skills yet. Put <code>.md</code> files in <code>workspace/agent/skills/</code>.", parse_mode=ParseMode.HTML)
        return
    lines = [f"- <b>{escape_html_text(s.name)}</b>: <i>{escape_html_text(', '.join(s.triggers))}</i>" for s in skills]
    await _answer_in_topic(message, "<b>Active skills:</b>\n" + "\n".join(lines), parse_mode=ParseMode.HTML)


def _build_approval_keyboard(event_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Run once", callback_data=f"approve:{event_id}"),
                InlineKeyboardButton(text="🔄 Allow this session", callback_data=f"allow_session:{event_id}"),
                InlineKeyboardButton(text="❌ Deny", callback_data=f"deny:{event_id}"),
            ]
        ]
    )


async def _telegram_approval_handler(event_id: str, tool_name: str, target: str, reason: str) -> str:
    """Return 'approved', 'session_allowed', or 'denied'."""
    event = asyncio.Event()
    _pending_approvals[event_id] = event

    logger.info("HITL approve? event=%s tool=%s reason=%s", event_id, tool_name, reason)
    await event.wait()

    result = _approval_results.pop(event_id, "denied")
    _pending_approvals.pop(event_id, None)
    logger.info("HITL result for %s: %s", event_id, result)
    return result


async def _typing_action_loop(bot: Any, chat_id: int, thread_id: int | None) -> None:
    """Send a 'typing…' chat action every ~4s until cancelled, so the user sees
    the agent is working before the first response chunk arrives."""
    try:
        while True:
            try:
                await bot.send_chat_action(chat_id=chat_id, action="typing", message_thread_id=thread_id)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        pass


def _build_clarify_keyboard(event_id: str, options: list[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for idx, opt in enumerate(options):
        label = opt if len(opt) <= 40 else opt[:39] + "…"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"clarify:{event_id}:{idx}", style="primary")])
    rows.append([InlineKeyboardButton(text="✏️ Other (type your own)", callback_data=f"clarify:{event_id}:other", style="secondary")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _telegram_clarify_handler(event_id: str, question: str, options: list[str]) -> str:
    """Wait for the user's clarify answer (button tap or typed reply), with timeout."""
    event = asyncio.Event()
    _pending_clarify[event_id] = event
    _clarify_options[event_id] = options
    logger.info("Awaiting clarify answer for event_id=%s", event_id)
    try:
        await asyncio.wait_for(event.wait(), timeout=_CLARIFY_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        logger.info("Clarify %s timed out", event_id)
    result = _clarify_results.pop(event_id, "")
    _pending_clarify.pop(event_id, None)
    _clarify_options.pop(event_id, None)
    return result or "[user did not answer; proceed with the most reasonable assumption]"


@router.message(F.document | F.photo)
async def handle_file_upload(message: Message, config: AppConfig, **kwargs: Any) -> None:
    """Accept file uploads (documents and photos), save to workspace, then run an agent turn."""
    if message.from_user is None:
        return

    from pathlib import Path

    uploads = Path(config.system.workspace_dir) / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)

    file_obj, name, size, mime = None, "", 0, ""

    if message.document:
        file_obj = message.document
        raw_name = (file_obj.file_name or f"doc_{file_obj.file_unique_id}").strip()
        # Sanitize: basename only, no path separators.
        name = Path(raw_name).name or f"doc_{file_obj.file_unique_id}"
        size = file_obj.file_size or 0
        mime = file_obj.mime_type or "application/octet-stream"
    elif message.photo:
        file_obj = message.photo[-1]  # largest size
        name = f"photo_{file_obj.file_unique_id}.jpg"
        size = file_obj.file_size or 0
        mime = "image/jpeg"

    if file_obj is None:
        return

    # Validate size before download (Telegram getFile also caps at ~20 MB).
    max_bytes = config.system.max_file_size_mb * 1024 * 1024
    if size > max_bytes:
        await _answer_in_topic(
            message,
            f"File too large ({size / 1024 / 1024:.1f} MB). Maximum: {config.system.max_file_size_mb} MB."
        )
        return
    if size > 20 * 1024 * 1024:
        await _answer_in_topic(message, "File exceeds Telegram's ~20 MB download limit.")
        return

    # Collision-safe target name.
    target = uploads / name
    if target.exists():
        stem, ext = target.stem, target.suffix
        idx = 1
        while target.exists():
            target = uploads / f"{stem}_{idx}{ext}"
            idx += 1

    try:
        await message.bot.download(file_obj, destination=str(target))
    except Exception as exc:  # noqa: BLE001
        logger.warning("File download failed: %s", exc)
        await _answer_in_topic(message, f"Could not download the file: {exc}")
        return

    caption = message.caption or ""
    # Build a synthetic user_message — includes the relative path so the agent
    # can reach it with read_file("uploads/name.pdf"), list_files("uploads/"), etc.
    hint = (
        f"The user uploaded a file saved at `uploads/{name}` (MIME: {mime}, {size / 1024:.0f} KB). "
        "You can read it with read_file('uploads/<filename>'), list the uploads directory, "
        "or extract content via execute_shell (e.g. pdftotext, unzip, python)."
    )
    if caption:
        hint += f"\nThe user's caption: {caption}"
    await _run_agent_turn(message, config, hint, **kwargs)


@router.message(F.text)
async def handle_user_message(message: Message, config: AppConfig, **kwargs: Any) -> None:
    """Handle private owner/allowed user text messages with draft streaming."""
    if message.text is None or message.from_user is None:
        return

    # If this chat owes a free-text clarify answer (user tapped "Other"), resolve
    # the pending clarify instead of starting a new agent turn.
    awaiting_cid = _clarify_awaiting_text.pop(message.chat.id, None)
    if awaiting_cid and awaiting_cid in _pending_clarify:
        _clarify_results[awaiting_cid] = message.text or ""
        _pending_clarify[awaiting_cid].set()
        return

    # Clear session-level approval cache for a new user message.
    _session_approved.clear()

    await _run_agent_turn(message, config, message.text, **kwargs)


async def _run_agent_turn(message: Message, config: AppConfig, user_text: str, **kwargs: Any) -> None:
    """Run one agent turn for *user_text* (shared by the text and file/photo handlers)."""
    chat_id = message.chat.id
    store = _session_store(config)
    topic_key = store.topic_key(message)
    history = store.get_history(message)
    current_task = asyncio.current_task()
    if current_task is not None:
        _active_tasks[topic_key] = current_task

    enable_draft = bool(config.telegram.enable_private_draft_streaming and message.chat.type == "private")
    buf = MessageBuffer(
        bot=message.bot,
        chat_id=chat_id,
        delay=config.telegram.stream_delay_seconds,
        enable_draft=enable_draft,
        send_kwargs=topic_send_kwargs(message),
        draft_kwargs=draft_topic_kwargs(message),
    )

    assistant_text = ""
    typing_task: asyncio.Task[Any] | None = None

    try:
        from core.agent import agent_loop

        owner_tools = kwargs.get("tools")
        if config.system.allow_file_send:
            owner_tools = attach_telegram_file_tool(
                owner_tools,
                bot=message.bot,
                chat_id=chat_id,
                max_file_size_mb=config.system.max_send_file_size_mb,
                send_kwargs=topic_send_kwargs(message),
            )

        scheduler = kwargs.get("scheduler")
        if scheduler is not None and owner_tools is not None:
            from core.scheduler import attach_schedule_tools
            owner_tools = attach_schedule_tools(owner_tools, scheduler, chat_id)

        # "typing…" indicator until the first response chunk arrives.
        typing_task = asyncio.create_task(
            _typing_action_loop(message.bot, chat_id, message.message_thread_id)
        )

        runtime_info = kwargs.get("runtime_info")

        generator = agent_loop(
            user_message=user_text,
            chat_history=history,
            tools=owner_tools,
            memory=kwargs.get("memory"),
            config=config,
            approval_handler=_telegram_approval_handler,
            clarify_handler=_telegram_clarify_handler,
            include_private_memory=True,
            allow_tools=True,
            allow_approval=True,
            runtime_info=runtime_info,
            session_cache=_session_approved,
        )

        async for event in generator:
            event_type = event.get("type", "")

            if event_type == "text":
                chunk = event.get("content", "") or ""
                if chunk:
                    if typing_task is not None:
                        typing_task.cancel()
                        typing_task = None
                    await buf.append(chunk)
                    assistant_text += chunk

            elif event_type == "action_start":
                tool = event.get("tool", "unknown")
                target = event.get("target", "")
                logger.info("⚙️ [%s] -> %s", tool, target)

            elif event_type == "action_result":
                output = event.get("output", "") or ""
                summary = output[:200] + ("…" if len(output) > 200 else "")
                logger.info("↳ Result: %s", summary)

            elif event_type == "pause_for_approval":
                await buf.flush()

                event_id = event.get("event_id", str(uuid.uuid4()))
                approval_tool = event.get("tool", "unknown")
                approval_target = event.get("target", "")
                approval_reason = event.get("reason", "")
                approval_text = (
                    f"🔐 <b>Approval Required</b>\n"
                    f"{format_action_blockquote(approval_tool, approval_target)}"
                    f"\n💡 <i>Reason: {escape_html_text(approval_reason)}</i>"
                    "\n\nDo you approve this action?"
                )

                try:
                    await _answer_in_topic(message, text=approval_text, parse_mode=ParseMode.HTML, reply_markup=_build_approval_keyboard(event_id))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed to send approval keyboard: %s", exc)

                buf = MessageBuffer(
                    bot=message.bot,
                    chat_id=chat_id,
                    delay=config.telegram.stream_delay_seconds,
                    enable_draft=enable_draft,
                    send_kwargs=topic_send_kwargs(message),
                    draft_kwargs=draft_topic_kwargs(message),
                )

            elif event_type == "clarify":
                await buf.flush()
                clarify_id = event.get("event_id", str(uuid.uuid4()))
                question = event.get("question", "") or "Could you clarify what you mean?"
                options = event.get("options") or []
                _clarify_options[clarify_id] = options
                try:
                    await _answer_in_topic(
                        message,
                        text=f"❓ {escape_html_text(question)}",
                        parse_mode=ParseMode.HTML,
                        reply_markup=_build_clarify_keyboard(clarify_id, options),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Failed to send clarify keyboard: %s", exc)

                buf = MessageBuffer(
                    bot=message.bot,
                    chat_id=chat_id,
                    delay=config.telegram.stream_delay_seconds,
                    enable_draft=enable_draft,
                    send_kwargs=topic_send_kwargs(message),
                    draft_kwargs=draft_topic_kwargs(message),
                )

            elif event_type == "error":
                error_msg = event.get("content", event.get("message", "Unknown error")) or "Unknown error"
                await buf.append("\n" + format_error_message(error_msg) + "\n")

            else:
                logger.warning("Unknown event type from agent_loop: %s", event_type)

        await buf.flush()

    except ImportError:
        logger.error("core.agent module not found")
        await buf.append("⚙️ The AI engine is not yet available.")
        await buf.flush()

    except asyncio.CancelledError:
        logger.info("Active task for topic %s was cancelled.", topic_key)
        await buf.append("\nTask stopped.")
        await buf.flush()
        return

    except Exception as exc:  # noqa: BLE001
        logger.exception("Unhandled error in message handler: %s", exc)
        await buf.append("\n" + format_error_message(str(exc)) + "\n")
        await buf.flush()

    finally:
        if typing_task is not None:
            typing_task.cancel()
        if current_task is not None and _active_tasks.get(topic_key) is current_task:
            _active_tasks.pop(topic_key, None)

    if assistant_text:
        history.append({"role": "assistant", "content": assistant_text})
        store.save_history(message, history)

@router.callback_query(F.data.startswith("approve:") | F.data.startswith("deny:") | F.data.startswith("allow_session:"))
async def handle_approval_callback(callback: CallbackQuery) -> None:
    if callback.data is None:
        await callback.answer("⚠️ Invalid callback data.")
        return

    parts = callback.data.split(":", 1)
    if len(parts) != 2:
        await callback.answer("⚠️ Malformed callback data.")
        return

    action, event_id = parts

    if action == "allow_session":
        _approval_results[event_id] = "session_allowed"
    else:
        _approval_results[event_id] = action == "approve"

    if event_id in _pending_approvals:
        _pending_approvals[event_id].set()
        status = "✅ Approved" if _approval_results[event_id] != "denied" else "❌ Denied"
        logger.info("HITL callback: %s for event_id=%s", status, event_id)
    else:
        status = "⚠️ Expired or unknown request"
        logger.warning("Received approval callback for unknown event_id=%s", event_id)

    await callback.answer(status)

    if callback.message is not None:
        try:
            await callback.message.edit_text(status, reply_markup=None)

            async def delayed_delete() -> None:
                await asyncio.sleep(3)
                try:
                    await callback.message.delete()
                except Exception as del_exc:  # noqa: BLE001
                    logger.warning("Failed to delete approval message: %s", del_exc)

            asyncio.create_task(delayed_delete())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to edit approval message: %s", exc)


@router.callback_query(F.data.startswith("clarify:"))
async def handle_clarify_callback(callback: CallbackQuery) -> None:
    if callback.data is None:
        await callback.answer("⚠️ Invalid callback data.")
        return

    parts = callback.data.split(":", 2)  # "clarify", event_id, choice
    if len(parts) != 3:
        await callback.answer("⚠️ Malformed callback data.")
        return

    _, event_id, choice = parts

    if choice == "other":
        if callback.message is not None:
            _clarify_awaiting_text[callback.message.chat.id] = event_id
            try:
                await callback.message.edit_text("✏️ OK, type your answer in this chat…", reply_markup=None)
            except Exception:  # noqa: BLE001
                pass
        await callback.answer("Type your answer in the chat.")
        return

    options = _clarify_options.get(event_id, [])
    try:
        answer = options[int(choice)]
    except (ValueError, IndexError):
        answer = choice

    _clarify_results[event_id] = answer
    if event_id in _pending_clarify:
        _pending_clarify[event_id].set()
        status = f"✅ {answer}"
    else:
        status = "⚠️ This question has expired."

    await callback.answer(status[:60])
    if callback.message is not None:
        try:
            await callback.message.edit_text(escape_html_text(status), reply_markup=None)
        except Exception:  # noqa: BLE001
            pass


@router.errors()
async def handle_router_error(event: Any) -> bool:
    exception = getattr(event, "exception", event)
    logger.exception("Unhandled exception in telegram handler: %s", exception)
    return True
