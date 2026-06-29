"""
Core agent logic and execution loop.

The owner chat uses the full workspace + tools flow. run_agent_once collects a
single final answer for internal/automated callers (sub-agents, scheduled jobs).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path
from typing import Any

from config.settings import AppConfig
from core.llm import stream_llm_response
from core.memory import WorkspaceMemory, auto_summarize_if_needed
from core.output_filter import UserVisibleStreamFilter
from core.shell_policy import is_command_allowlisted, is_command_blocked

_SESSION_KEY_SEP: str = "::"
from core.skills import build_skills_context, load_skills_dir, match_skills
from core.tools import ToolRegistry

logger = logging.getLogger(__name__)

_VISIBLE_OUTPUT_POLICY = """
OUTPUT CONTRACT & AGENT LOOP — STRICT:
You are Nano-Agent, an autonomous AI agent. You do not just answer questions; you take action.

1. USER-VISIBLE OUTPUT:
- Reply in the same language the user writes in (match their language naturally).
- Assistant message content is public {channel} text. Put only final answers or concise user-facing status updates there.
- Never write hidden reasoning, chain-of-thought, prompt interpretation, tool-result narration, or planning notes in assistant content.
- Do not use <think>, <thought>, <reasoning>, <analysis>, or similar tags. The UI must not be responsible for hiding your reasoning.
- If you need workspace facts or tool results, call tools first. After tool results arrive, answer with only the final result.

2. PROACTIVE WORKSPACE, MEMORY & LEARNING:
- You have a persistent workspace (your home). The WORKSPACE SNAPSHOT section shows what is in it right now — you already know your own files.
- You MUST proactively use tools to write and update state files whenever you learn new information or make progress.
- When you learn something durable about the owner, record it: `update_user_fact` for stable facts/preferences, `update_project_memory` for project decisions. This is how you develop and improve over time.
- DO NOT ask for permission to write to your own workspace. Just do it in the background, and then inform the user it is done.

3. AGENT-ENVIRONMENT LOOP:
- If a task is not complete, use tools to make progress. Do NOT provide a final answer until the task is fully resolved or you are completely stuck.
- When you use a tool, wait for the result. Do not guess the result.
- For greetings: reply briefly and naturally.
- For commands: execute them, write logs to the workspace if needed, and report the final result concisely.

4. SELF-AWARENESS — you already know how you are built, so do not rediscover it every turn:
- You are a Python Telegram bot (aiogram) running this agent loop over several LLM providers with automatic fallback. Settings live in config.json.
- Your capabilities ARE the tools under "YOUR AVAILABLE TOOLS" above. That list is generated live and is the source of truth: if a tool is not listed, you cannot do it — never invent or assume a tool exists.
- Your memory is the workspace Markdown files (SOUL, IDENTITY, USER, RELATIONSHIP, MEMORY, and daily memory/YYYY-MM-DD.md). Their current contents are already loaded in the sections above. Update them with your file tools.
- Skills are keyword-triggered Markdown playbooks in <workspace>/skills/*.md; relevant ones are auto-injected above. To add one, write a new .md there. MCP servers, if any, already add their tools to the list above.
- ACT on what is above. Do NOT spend turns reading source files just to rediscover features you already have. Read a file only for a concrete detail you genuinely lack (ROADMAP.md for planned features, config.json for exact settings) — read the ONE file you need, then act.
"""

def _requires_tool_approval(tool_name: str, args: dict[str, Any], destructive: bool, config: AppConfig) -> bool:
    """Decide whether a tool call must pause for human approval."""
    if tool_name == "execute_shell":
        command = str(args.get("command", ""))

        # Blocklist wins over everything: blocked commands always need approval.
        if is_command_blocked(command, config.system.exec_blocked_commands):
            return True

        mode = config.system.exec_approval_mode
        if mode == "all":
            return False
        if mode == "list":
            return not is_command_allowlisted(command, config.system.exec_allowed_commands)
        return True

    return destructive


def _approval_reason(tool_name: str, args: dict[str, Any], config: AppConfig, tool_instance: Any) -> str:
    """Build a human-readable reason why this tool call needs approval."""
    if tool_name == "execute_shell":
        command = str(args.get("command", ""))
        if is_command_blocked(command, config.system.exec_blocked_commands):
            return f"'{command}' is on the blocklist of dangerous commands."
        mode = config.system.exec_approval_mode
        if mode == "ask":
            return f"Shell approval mode is always-ask ('{mode}')."
        if mode == "list" and not is_command_allowlisted(command, config.system.exec_allowed_commands):
            return f"'{command}' is not in the allowlist."
        return "This shell command is destructive."
    if getattr(tool_instance, "destructive", False):
        name = tool_name.replace("_", " ")
        return f"'{name}' modifies or deletes files — always requires confirmation."
    return "This action requires your approval."


async def agent_loop(
    user_message: str,
    chat_history: list[dict[str, Any]],
    tools: ToolRegistry | None,
    memory: WorkspaceMemory,
    config: AppConfig,
    approval_handler: Callable[[str, str, str, str], Awaitable[str]] | None = None,
    *,
    include_private_memory: bool = True,
    allow_tools: bool = True,
    allow_approval: bool = True,
    channel_name: str = "Telegram",
    clarify_handler: Callable[[str, str, list[str]], Awaitable[str]] | None = None,
    runtime_info: str | None = None,
    session_cache: dict[str, bool] | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Run the core agent loop and yield channel-facing events.

    Yield types:
    - ``{"type": "text", "content": "..."}``
    - ``{"type": "action_start", "tool": "...", "target": "..."}``
    - ``{"type": "action_result", "tool": "...", "output": "..."}``
    - ``{"type": "pause_for_approval", "event_id": "...", "tool": "...", "target": "...", "reason": "..."}``
    - ``{"type": "error", "content": "..."}``
    """
    try:
        system_context_text = await memory.build_system_context()

        # Inject the agent's own tool roster into context so it knows its capabilities.
        if allow_tools and tools is not None:
            names = tools.list_names()
            system_context_text += f"\n\n## YOUR AVAILABLE TOOLS ({len(names)} total)\n"
            for n in names:
                t = tools.get(n)
                if t:
                    tag = " ⚠️ DESTRUCTIVE (requires approval)" if getattr(t, "destructive", False) else ""
                    system_context_text += f"- `{n}`{tag}: {t.description[:120]}\n"

        # Inject runtime environment info so the agent knows its hosting context.
        if runtime_info:
            system_context_text += f"\n\n## YOUR HOSTING ENVIRONMENT\n{runtime_info}\n"

        # Inject keyword-triggered skills. Skills live in <workspace_dir>/skills/*.md.
        matched = match_skills(
            user_message,
            load_skills_dir(Path(config.system.workspace_dir) / "skills"),
        )
        if matched:
            system_context_text += build_skills_context(matched)

        system_context_text = f"{system_context_text.rstrip()}{_VISIBLE_OUTPUT_POLICY.format(channel=channel_name)}"

        system_message = {"role": "system", "content": system_context_text}

        chat_history.append({"role": "user", "content": user_message})
        messages: list[dict[str, Any]] = [system_message] + chat_history

        if include_private_memory:
            messages = await auto_summarize_if_needed(
                messages=messages,
                memory=memory,
                providers=config.llm.providers,
                threshold=config.llm.summarization_threshold_tokens,
            )

        iteration = 0
        max_iterations = config.llm.max_iterations

        while iteration < max_iterations:
            iteration += 1
            # On the final allowed iteration, withhold tools so the model stops
            # exploring and must give the user a real answer — otherwise a stuck
            # agent burns every iteration on tool calls and the user gets nothing.
            final_iteration = iteration >= max_iterations
            tool_schemas = (
                tools.to_schemas()
                if allow_tools and tools is not None and not final_iteration
                else None
            )
            if final_iteration and allow_tools and tools is not None:
                messages.append({
                    "role": "system",
                    "content": (
                        "You have reached your tool-use limit for this turn. Do not call tools. "
                        "Answer the user now using what you already have, and state briefly what "
                        "is still missing or unverified if the task is not fully done."
                    ),
                })

            # Context Window Management: Ensure we don't blow up the context window during long loops
            if include_private_memory and iteration > 1:
                messages = await auto_summarize_if_needed(
                    messages=messages,
                    memory=memory,
                    providers=config.llm.providers,
                    threshold=config.llm.summarization_threshold_tokens,
                )

            logger.debug("Agent loop iteration %d. Calling LLM...", iteration)

            stream = stream_llm_response(
                messages=messages,
                tools=tool_schemas,
                providers=config.llm.providers,
            )

            output_filter = UserVisibleStreamFilter()
            llm_text = ""
            raw_llm_text = ""
            tool_calls_to_execute: list[dict[str, Any]] = []

            async for chunk in stream:
                chunk_type = chunk.get("type")

                if chunk_type == "text_delta":
                    raw_content = chunk.get("content", "") or ""
                    raw_llm_text += raw_content
                    for visible in output_filter.feed(raw_content):
                        if visible:
                            llm_text += visible
                            yield {"type": "text", "content": visible}

                elif chunk_type == "tool_call":
                    if allow_tools:
                        tool_calls_to_execute.append(
                            {
                                "id": chunk.get("id"),
                                "name": chunk.get("name"),
                                "arguments": chunk.get("arguments"),
                            }
                        )
                    else:
                        logger.warning("Dropped tool call on no-tools surface: %s", chunk.get("name"))

                elif chunk_type == "error":
                    for visible in output_filter.flush():
                        if visible:
                            llm_text += visible
                            yield {"type": "text", "content": visible}
                    yield {"type": "error", "content": chunk.get("content", "Unknown LLM error")}
                    return

            visible_parts = [visible for visible in output_filter.flush() if visible]
            if visible_parts and not tool_calls_to_execute:
                for visible in visible_parts:
                    llm_text += visible
                    yield {"type": "text", "content": visible}
            elif visible_parts and tool_calls_to_execute:
                logger.debug("Suppressed assistant pre-tool text on silent tool turn.")

            if raw_llm_text.strip() and not llm_text and not tool_calls_to_execute:
                fallback_text = (
                    "I don't have a safe final answer to show yet. "
                    "Please rephrase or try again."
                )
                llm_text = fallback_text
                yield {"type": "text", "content": fallback_text}

            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if llm_text and not tool_calls_to_execute:
                assistant_msg["content"] = llm_text
            if tool_calls_to_execute:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.get("id") or str(uuid.uuid4()),
                        "type": "function",
                        "function": {
                            "name": tc.get("name") or "",
                            "arguments": tc.get("arguments") or "{}",
                        },
                    }
                    for tc in tool_calls_to_execute
                ]

            messages.append(assistant_msg)

            if not tool_calls_to_execute:
                break

            if tools is None:
                yield {"type": "error", "content": "Tool call requested, but tools are not available on this surface."}
                break

            pending_tasks = []
            tool_contexts = []

            # Phase 1: Sequential Approval and Task Preparation
            for tc in tool_calls_to_execute:
                tool_name = tc.get("name") or ""
                args_str = tc.get("arguments") or "{}"
                tool_id = tc.get("id") or str(uuid.uuid4())

                try:
                    args = json.loads(args_str)
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    args = {}

                # Special tool: ask_user (clarify). Handled by the loop + channel,
                # never executed as a normal tool.
                if tool_name == "ask_user":
                    question = str(args.get("question", "")).strip() or "Could you clarify what you mean?"
                    raw_opts = args.get("options")
                    options = [str(o) for o in raw_opts][:6] if isinstance(raw_opts, list) else []
                    clarify_id = str(uuid.uuid4())
                    yield {"type": "clarify", "event_id": clarify_id, "question": question, "options": options}
                    if clarify_handler and allow_approval:
                        answer = await clarify_handler(clarify_id, question, options)
                    else:
                        answer = "[clarify unavailable here; proceed with the most reasonable assumption]"
                    yield {"type": "action_result", "tool": "ask_user", "output": answer}
                    messages.append({"role": "tool", "tool_call_id": tool_id, "name": "ask_user", "content": answer})
                    continue

                tool_instance = tools.get(tool_name)

                if not tool_instance:
                    error_msg = f"Tool '{tool_name}' not found in registry."
                    yield {"type": "action_result", "tool": tool_name, "output": error_msg}
                    messages.append({"role": "tool", "tool_call_id": tool_id, "name": tool_name, "content": error_msg})
                    continue

                target_summary = json.dumps(args, ensure_ascii=False)[:1000]

                requires_approval = _requires_tool_approval(
                    tool_name=tool_name,
                    args=args,
                    destructive=bool(getattr(tool_instance, "destructive", False)),
                    config=config,
                )

                # Build a human-readable reason for the approval prompt.
                reason = _approval_reason(tool_name, args, config, tool_instance)

                # Check if this tool+args was session-approved earlier.
                session_key = f"{tool_name}{_SESSION_KEY_SEP}{target_summary[:100]}"
                if requires_approval and session_cache and session_key in session_cache:
                    requires_approval = False
                    target_summary = f"{target_summary} (session-allowed)"

                approved = True
                if requires_approval and approval_handler and allow_approval:
                    event_id = str(uuid.uuid4())
                    yield {
                        "type": "pause_for_approval",
                        "event_id": event_id,
                        "tool": tool_name,
                        "target": target_summary,
                        "reason": reason,
                    }
                    result = await approval_handler(event_id, tool_name, target_summary, reason)
                    if result == "session_allowed":
                        if session_cache is not None:
                            session_cache[session_key] = True
                        approved = True
                    elif result == "approved":
                        approved = True
                    else:
                        approved = False
                    if not approved:
                        deny_msg = "User denied execution of this tool."
                        yield {"type": "action_result", "tool": tool_name, "output": deny_msg}
                        messages.append({"role": "tool", "tool_call_id": tool_id, "name": tool_name, "content": deny_msg})
                        continue
                elif requires_approval and not (approval_handler and allow_approval):
                    deny_msg = "Tool execution requires approval, but approval is not available on this surface."
                    yield {"type": "action_result", "tool": tool_name, "output": deny_msg}
                    messages.append({"role": "tool", "tool_call_id": tool_id, "name": tool_name, "content": deny_msg})
                    continue

                yield {"type": "action_start", "tool": tool_name, "target": target_summary}

                tool_contexts.append((tool_id, tool_name))
                pending_tasks.append(asyncio.create_task(tool_instance.execute(**args)))

            # Phase 2: Concurrent Execution
            if pending_tasks:
                results = await asyncio.gather(*pending_tasks, return_exceptions=True)

                for (tool_id, tool_name), result in zip(tool_contexts, results):
                    if isinstance(result, Exception):
                        logger.exception("Tool %s failed", tool_name, exc_info=result)
                        result_str = f"Tool error: {type(result).__name__}: {result}"
                    else:
                        result_str = str(result)

                    # Truncate tool output sent back to LLM to preserve context stability
                    max_tool_output_len = 8000
                    if len(result_str) > max_tool_output_len:
                        result_str = result_str[:max_tool_output_len] + "\n...[Output Truncated]"

                    result_preview = result_str[:500] + ("…" if len(result_str) > 500 else "")
                    yield {"type": "action_result", "tool": tool_name, "output": result_preview}
                    messages.append({"role": "tool", "tool_call_id": tool_id, "name": tool_name, "content": result_str})

        else:
            logger.warning("Agent loop hit max iterations (%d).", max_iterations)
            yield {"type": "error", "content": "Agent reached maximum iteration limit."}

    except Exception as exc:  # noqa: BLE001
        logger.exception("Agent loop failed")
        yield {"type": "error", "content": f"Agent encountered an internal error: {exc}"}


async def run_agent_once(
    user_message: str,
    *,
    tools: ToolRegistry | None,
    memory: WorkspaceMemory,
    config: AppConfig,
    include_private_memory: bool,
    allow_tools: bool,
    channel_name: str = "Telegram",
) -> str:
    """Collect a single final answer from ``agent_loop`` (sub-agents, scheduled jobs)."""
    chunks: list[str] = []
    async for event in agent_loop(
        user_message=user_message,
        chat_history=[],
        tools=tools if allow_tools else None,
        memory=memory,
        config=config,
        approval_handler=None,
        include_private_memory=include_private_memory,
        allow_tools=allow_tools,
        allow_approval=False,
        channel_name=channel_name,
    ):
        if event.get("type") == "text":
            chunks.append(event.get("content", "") or "")
        elif event.get("type") == "error":
            chunks.append("\n" + (event.get("content", "") or "Unknown error"))
    return "".join(chunks).strip()
