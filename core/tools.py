"""
Tool registry and built-in tool implementations for the AI Agent Framework.

Provides :class:`BaseTool` (abstract) and :class:`ToolRegistry` along with six
production-ready built-in tools:

* ExecuteShellTool — sandboxed async shell execution
* ReadFileTool — async file reader with size guard
* WriteFileTool — async file writer / appender
* WebSearchTool — DuckDuckGo search via ``duckduckgo_search``
* UpdateUserFactTool — append facts to USER.md
* UpdateProjectMemoryTool — append notes to MEMORY.md
"""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from typing import Any

import aiofiles

logger = logging.getLogger(__name__)

_DEFAULT_SHELL_TIMEOUT: float = 60.0
_MAX_OUTPUT_BYTES: int = 50 * 1024  # 50 KB cap on tool output
_DEFAULT_MAX_FILE_SIZE_MB: int = 50


class BaseTool(ABC):
    """Abstract base class for every tool the agent can invoke.

    Subclasses **must** override :pymethod:`execute` and populate the class-level
    ``name``, ``description``, and ``parameters`` attributes.
    """

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}  # JSON Schema (OpenAI function-calling format)
    destructive: bool = False  # If True the HITL layer must approve before execution

    def to_schema(self) -> dict[str, Any]:
        """Return an OpenAI function-calling compatible tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    @abstractmethod
    async def execute(self, **params: Any) -> str:
        """Execute the tool with the given keyword arguments.

        Returns a human-readable result string.
        """
        ...


class ToolRegistry:
    """Thread-safe registry mapping tool names → :class:`BaseTool` instances."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        """Register *tool* under its ``name``. Overwrites silently."""
        if not tool.name:
            raise ValueError("Tool must have a non-empty 'name' attribute.")
        self._tools[tool.name] = tool
        logger.debug("Registered tool: %s (destructive=%s)", tool.name, tool.destructive)

    def get(self, name: str) -> BaseTool | None:
        """Return the tool registered under *name*, or ``None``."""
        return self._tools.get(name)

    def to_schemas(self) -> list[dict[str, Any]]:
        """Return OpenAI-compatible schemas for all registered tools."""
        return [tool.to_schema() for tool in self._tools.values()]

    def list_names(self) -> list[str]:
        """Return a sorted list of registered tool names."""
        return sorted(self._tools.keys())

    def clone(self) -> "ToolRegistry":
        """Return a shallow copy of this registry.

        This is used when a request needs a temporary per-chat/per-user tool,
        such as a per-request send-file tool, without mutating the global tool registry.

        Tool instances are intentionally shared. Built-in tools are effectively
        stateless or config-bound, so a shallow copy is enough here.
        """
        registry = ToolRegistry()
        registry._tools = dict(self._tools)
        return registry


def _safe_resolve(path: str, allowed_root: str | None = None) -> str:
    """Resolve *path* and guard against path-traversal.

    If *allowed_root* is provided the resolved path **must** start with it
    (after appending ``os.sep`` to prevent partial-prefix bypass, e.g.
    ``/sandbox-malicious`` should not pass a ``/sandbox`` check).

    Returns the resolved absolute path string.

    Raises:
        PermissionError: When the resolved path escapes *allowed_root*.
    """
    resolved = os.path.realpath(os.path.expanduser(path))
    if allowed_root is not None:
        root = os.path.realpath(allowed_root)
        # Enforce trailing separator to avoid partial-prefix bypass
        # TODO(security): consider additional symlink-following checks
        if not (resolved == root or resolved.startswith(root + os.sep)):
            raise PermissionError(
                f"Path '{path}' resolves to '{resolved}' which is outside "
                f"the allowed root '{root}'."
            )
    return resolved


def _resolve_file_path(path: str, workspace_dir: str | None = None) -> str:
    """Resolve a user/model file path with workspace-friendly relative paths.

    Existing project-relative paths keep working. New relative paths default to
    the agent workspace so files such as ``USER.md`` and ``notes.md`` land under
    ``workspace/agent`` instead of the repository root.
    """
    expanded = os.path.expanduser(path)
    if os.path.isabs(expanded) or workspace_dir is None:
        return _safe_resolve(expanded)

    cwd_candidate = _safe_resolve(expanded)
    if os.path.exists(cwd_candidate) or expanded == "workspace" or expanded.startswith("workspace" + os.sep):
        return cwd_candidate

    return _safe_resolve(os.path.join(workspace_dir, expanded))


def _cap_output(text: str, max_bytes: int = _MAX_OUTPUT_BYTES) -> str:
    """Truncate *text* to at most *max_bytes* UTF-8 bytes."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated + "\n... [output truncated to 50 KB]"


class ExecuteShellTool(BaseTool):
    """Execute a shell command asynchronously with timeout and output cap."""

    name = "execute_shell"
    description = (
        "Execute a shell command on the local system. "
        "Returns stdout and stderr. Commands time out after 60 seconds. "
        "Output is capped at 50 KB."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute.",
            },
        },
        "required": ["command"],
    }
    destructive = True

    async def execute(self, **params: Any) -> str:  # noqa: D401
        command: str = params.get("command", "")
        if not command.strip():
            return "Error: empty command."

        logger.info("ExecuteShellTool: running command")
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=_DEFAULT_SHELL_TIMEOUT
            )
        except asyncio.TimeoutError:
            # Best-effort kill — the process may already be gone.
            try:
                proc.kill()  # type: ignore[union-attr]
            except ProcessLookupError:
                pass
            return "Error: command timed out after 60 seconds."
        except Exception as exc:
            logger.exception("ExecuteShellTool error")
            return f"Error executing command: {exc}"

        stdout_str = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        stderr_str = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

        parts: list[str] = []
        if stdout_str:
            parts.append(f"STDOUT:\n{stdout_str}")
        if stderr_str:
            parts.append(f"STDERR:\n{stderr_str}")
        parts.append(f"Exit code: {proc.returncode}")

        return _cap_output("\n".join(parts))


class ReadFileTool(BaseTool):
    """Asynchronously read a file from disk."""

    name = "read_file"
    description = (
        "Read the contents of a file at the given path. "
        "Relative paths that do not already exist resolve inside the agent workspace. "
        "Maximum file size is determined by config (default 50 MB)."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute or relative path to the file to read.",
            },
        },
        "required": ["file_path"],
    }
    destructive = False

    def __init__(
        self,
        max_file_size_mb: int = _DEFAULT_MAX_FILE_SIZE_MB,
        workspace_dir: str | None = None,
    ) -> None:
        self._max_bytes = max_file_size_mb * 1024 * 1024
        self._workspace_dir = workspace_dir

    async def execute(self, **params: Any) -> str:
        file_path: str = params.get("file_path", "")
        if not file_path:
            return "Error: file_path is required."

        try:
            resolved = _resolve_file_path(file_path, self._workspace_dir)
        except PermissionError as exc:
            return str(exc)

        if not os.path.isfile(resolved):
            return f"Error: '{resolved}' does not exist or is not a file."

        size = os.path.getsize(resolved)
        if size > self._max_bytes:
            return (
                f"Error: file is {size / (1024 * 1024):.1f} MB, "
                f"exceeds limit of {self._max_bytes / (1024 * 1024):.0f} MB."
            )

        try:
            async with aiofiles.open(resolved, mode="r", encoding="utf-8", errors="replace") as fh:
                content = await fh.read()
        except Exception as exc:
            logger.exception("ReadFileTool error")
            return f"Error reading file: {exc}"

        return _cap_output(content)


class WriteFileTool(BaseTool):
    """Asynchronously write or append to a file."""

    name = "write_file"
    description = (
        "Write content to a file. Supports 'write' (overwrite) and 'append' "
        "modes. Relative paths resolve inside the agent workspace unless they "
        "already exist in the project. Parent directories are created automatically."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute or relative path to the file.",
            },
            "content": {
                "type": "string",
                "description": "The text content to write.",
            },
            "mode": {
                "type": "string",
                "enum": ["write", "append"],
                "description": "Write mode: 'write' to overwrite, 'append' to add. Default: 'write'.",
                "default": "write",
            },
        },
        "required": ["file_path", "content"],
    }
    destructive = True

    def __init__(self, workspace_dir: str | None = None) -> None:
        self._workspace_dir = workspace_dir

    async def execute(self, **params: Any) -> str:
        file_path: str = params.get("file_path", "")
        content: str = params.get("content", "")
        mode: str = params.get("mode", "write")

        if not file_path:
            return "Error: file_path is required."
        if mode not in ("write", "append"):
            return f"Error: mode must be 'write' or 'append', got '{mode}'."

        try:
            resolved = _resolve_file_path(file_path, self._workspace_dir)
        except PermissionError as exc:
            return str(exc)

        parent_dir = os.path.dirname(resolved)
        os.makedirs(parent_dir, exist_ok=True)

        file_mode = "w" if mode == "write" else "a"
        try:
            async with aiofiles.open(resolved, mode=file_mode, encoding="utf-8") as fh:
                await fh.write(content)
        except Exception as exc:
            logger.exception("WriteFileTool error")
            return f"Error writing file: {exc}"

        action = "written to" if mode == "write" else "appended to"
        return f"Successfully {action} '{resolved}' ({len(content)} chars)."

class ReplaceInFileTool(BaseTool):
    """Asynchronously replace a specific string block in a file."""

    name = "replace_in_file"
    description = (
        "Replace a specific block of text in a file with new text. "
        "Relative paths resolve inside the agent workspace unless they already "
        "exist in the project. "
        "The old_text MUST exactly match what is currently in the file."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute or relative path to the file.",
            },
            "old_text": {
                "type": "string",
                "description": "Exact text to replace.",
            },
            "new_text": {
                "type": "string",
                "description": "New text to insert.",
            },
        },
        "required": ["file_path", "old_text", "new_text"],
    }
    destructive = True

    def __init__(self, workspace_dir: str | None = None) -> None:
        self._workspace_dir = workspace_dir

    async def execute(self, **params: Any) -> str:
        file_path: str = params.get("file_path", "")
        old_text: str = params.get("old_text", "")
        new_text: str = params.get("new_text", "")

        if not file_path or not old_text:
            return "Error: file_path and old_text are required."

        try:
            resolved = _resolve_file_path(file_path, self._workspace_dir)
        except PermissionError as exc:
            return str(exc)

        if not os.path.isfile(resolved):
            return f"Error: File '{resolved}' does not exist or is not a file."

        try:
            async with aiofiles.open(resolved, mode="r", encoding="utf-8") as fh:
                content = await fh.read()

            if old_text not in content:
                return "Error: old_text not found in the file. Ensure an exact match including all whitespaces."

            count = content.count(old_text)
            content = content.replace(old_text, new_text)

            async with aiofiles.open(resolved, mode="w", encoding="utf-8") as fh:
                await fh.write(content)

            return f"Successfully replaced {count} occurrence(s) in '{resolved}'."
        except Exception as exc:
            logger.exception("ReplaceInFileTool error")
            return f"Error replacing in file: {exc}"


class WebSearchTool(BaseTool):
    """Search the web using DuckDuckGo (via ``duckduckgo_search``)."""

    name = "web_search"
    description = (
        "Search the web. Uses DuckDuckGo, falling back to Brave then Tavily when "
        "those API keys are configured. Returns top results with title, URL, and snippet."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query.",
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results to return (default 5).",
                "default": 5,
            },
        },
        "required": ["query"],
    }
    destructive = False

    async def execute(self, **params: Any) -> str:
        query: str = params.get("query", "")
        max_results: int = int(params.get("max_results", 5))
        if not query:
            return "Error: query is required."

        from core.search import search
        try:
            return await search(query, max_results)
        except Exception as exc:  # noqa: BLE001
            logger.exception("WebSearchTool error")
            return f"Error during web search: {exc}"


class UpdateUserFactTool(BaseTool):
    """Append a learned fact about the user to workspace/agent/USER.md."""

    name = "update_user_fact"
    description = (
        "Record a fact about the user in USER.md under the '## Facts' section. "
        "Use this when you learn something new about the user."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "fact": {
                "type": "string",
                "description": "The fact to record about the user.",
            },
        },
        "required": ["fact"],
    }
    destructive = False

    def __init__(self, workspace_dir: str = "workspace/agent") -> None:
        self._workspace_dir = workspace_dir

    async def execute(self, **params: Any) -> str:
        fact: str = params.get("fact", "").strip()
        if not fact:
            return "Error: fact is required."

        user_file = os.path.join(self._workspace_dir, "USER.md")
        resolved = _safe_resolve(user_file)

        try:
            content = ""
            if os.path.isfile(resolved):
                async with aiofiles.open(resolved, mode="r", encoding="utf-8") as fh:
                    content = await fh.read()

            marker = "## Facts"
            if marker in content:
                idx = content.index(marker) + len(marker)
                # Insert after the marker line (past newline)
                newline_idx = content.find("\n", idx)
                if newline_idx == -1:
                    newline_idx = len(content)
                new_content = (
                    content[: newline_idx + 1]
                    + f"\n- {fact}\n"
                    + content[newline_idx + 1:]
                )
            else:
                new_content = content.rstrip() + f"\n\n## Facts\n\n- {fact}\n"

            async with aiofiles.open(resolved, mode="w", encoding="utf-8") as fh:
                await fh.write(new_content)
        except Exception as exc:
            logger.exception("UpdateUserFactTool error")
            return f"Error updating user facts: {exc}"

        return f"Recorded user fact: {fact}"


class UpdateProjectMemoryTool(BaseTool):
    """Append content to workspace/agent/MEMORY.md under a given section."""

    name = "update_project_memory"
    description = (
        "Record information in MEMORY.md under a specified section heading. "
        "Use this to persist project context, environment details, or important notes."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The content to record.",
            },
            "section": {
                "type": "string",
                "description": "The '## Section' heading to append under (default: 'Important Notes').",
                "default": "Important Notes",
            },
        },
        "required": ["content"],
    }
    destructive = False

    def __init__(self, workspace_dir: str = "workspace/agent") -> None:
        self._workspace_dir = workspace_dir

    async def execute(self, **params: Any) -> str:
        new_content: str = params.get("content", "").strip()
        section: str = params.get("section", "Important Notes").strip()
        if not new_content:
            return "Error: content is required."

        memory_file = os.path.join(self._workspace_dir, "MEMORY.md")
        resolved = _safe_resolve(memory_file)

        try:
            file_content = ""
            if os.path.isfile(resolved):
                async with aiofiles.open(resolved, mode="r", encoding="utf-8") as fh:
                    file_content = await fh.read()

            marker = f"## {section}"
            if marker in file_content:
                idx = file_content.index(marker) + len(marker)
                newline_idx = file_content.find("\n", idx)
                if newline_idx == -1:
                    newline_idx = len(file_content)
                updated = (
                    file_content[: newline_idx + 1]
                    + f"\n- {new_content}\n"
                    + file_content[newline_idx + 1:]
                )
            else:
                updated = file_content.rstrip() + f"\n\n## {section}\n\n- {new_content}\n"

            async with aiofiles.open(resolved, mode="w", encoding="utf-8") as fh:
                await fh.write(updated)
        except Exception as exc:
            logger.exception("UpdateProjectMemoryTool error")
            return f"Error updating project memory: {exc}"

        return f"Recorded in [{section}]: {new_content}"


class ListFilesTool(BaseTool):
    """List files/folders at a path, optionally filtered by a glob pattern."""

    name = "list_files"
    description = (
        "List files and folders at a path. Optionally filter with a glob pattern like "
        "'*.py'. Relative paths resolve inside the agent workspace. Use this to discover "
        "what files exist before reading or editing them."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path (default: workspace root)."},
            "pattern": {"type": "string", "description": "Optional glob, e.g. '*.md'."},
        },
    }
    destructive = False

    def __init__(self, workspace_dir: str | None = None) -> None:
        self._workspace_dir = workspace_dir

    async def execute(self, **params: Any) -> str:
        path = params.get("path") or "."
        pattern = params.get("pattern") or ""
        try:
            resolved = _resolve_file_path(path, self._workspace_dir)
        except PermissionError as exc:
            return str(exc)
        if not os.path.isdir(resolved):
            return f"Error: '{resolved}' is not a directory."
        try:
            if pattern:
                import glob as _glob
                names = sorted(os.path.basename(p) for p in _glob.glob(os.path.join(resolved, pattern)))
            else:
                names = sorted(os.listdir(resolved))
        except OSError as exc:
            return f"Error listing directory: {exc}"
        if not names:
            return f"(empty) {resolved}"
        lines = [f"{n}/" if os.path.isdir(os.path.join(resolved, n)) else n for n in names[:500]]
        more = "" if len(names) <= 500 else f"\n… and {len(names) - 500} more"
        return f"{resolved}:\n" + "\n".join(lines) + more


class MakeDirectoryTool(BaseTool):
    """Create a directory (and any parents)."""

    name = "make_directory"
    description = "Create a directory (and parent directories). Relative paths resolve inside the agent workspace."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Directory path to create."}},
        "required": ["path"],
    }
    destructive = False

    def __init__(self, workspace_dir: str | None = None) -> None:
        self._workspace_dir = workspace_dir

    async def execute(self, **params: Any) -> str:
        path = params.get("path", "")
        if not path:
            return "Error: path is required."
        try:
            resolved = _resolve_file_path(path, self._workspace_dir)
            os.makedirs(resolved, exist_ok=True)
        except (OSError, PermissionError) as exc:
            return f"Error creating directory: {exc}"
        return f"Created directory '{resolved}'."


class DeleteFileTool(BaseTool):
    """Delete a single file (destructive — requires approval)."""

    name = "delete_file"
    description = "Delete a file. Relative paths resolve inside the agent workspace. This is destructive and requires approval."
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"file_path": {"type": "string", "description": "Path of the file to delete."}},
        "required": ["file_path"],
    }
    destructive = True

    def __init__(self, workspace_dir: str | None = None) -> None:
        self._workspace_dir = workspace_dir

    async def execute(self, **params: Any) -> str:
        file_path = params.get("file_path", "")
        if not file_path:
            return "Error: file_path is required."
        try:
            resolved = _resolve_file_path(file_path, self._workspace_dir)
        except PermissionError as exc:
            return str(exc)
        if not os.path.isfile(resolved):
            return f"Error: '{resolved}' is not a file."
        try:
            os.remove(resolved)
        except OSError as exc:
            return f"Error deleting file: {exc}"
        return f"Deleted '{resolved}'."


class FetchUrlTool(BaseTool):
    """Fetch a URL over HTTP GET and return its text content."""

    name = "fetch_url"
    description = (
        "Fetch the content of a URL via HTTP GET and return it as text (HTML is stripped "
        "to readable text). Use to read a web page or a JSON/API endpoint. Output is capped."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute http(s) URL to fetch."},
        },
        "required": ["url"],
    }
    destructive = False

    async def execute(self, **params: Any) -> str:
        url = str(params.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            return "Error: url must start with http:// or https://"
        import re

        import aiohttp

        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url, headers={"User-Agent": "Nano-Agent/0.1.0"}) as resp:
                    status = resp.status
                    body = await resp.text()
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            return f"Error fetching {url}: {type(exc).__name__}: {exc}"

        # Crude HTML → text: drop script/style, strip tags, collapse whitespace.
        body = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body, flags=re.S | re.I)
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"\s+", " ", body).strip()
        return _cap_output(f"HTTP {status} — {url}\n\n{body}")


class GetCurrentTimeTool(BaseTool):
    """Return the current local date and time."""

    name = "get_current_time"
    description = "Get the current local date and time. Use for timestamps, scheduling, or 'what day/time is it'."
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    destructive = False

    async def execute(self, **params: Any) -> str:
        from datetime import datetime

        now = datetime.now()
        return f"Current local time: {now.isoformat(timespec='seconds')} ({now.strftime('%A, %d %B %Y, %H:%M')})"


class AskUserTool(BaseTool):
    """Ask the user a clarifying question (handled by the agent loop + channel)."""

    name = "ask_user"
    description = (
        "Ask the user a clarifying question when their request is ambiguous, instead "
        "of guessing. Give a short question and optional answer options (shown as "
        "buttons). Returns the user's choice or typed reply. Prefer this over assuming."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The clarifying question to ask."},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional short answer choices to show as buttons.",
            },
        },
        "required": ["question"],
    }
    destructive = False

    async def execute(self, **params: Any) -> str:
        # The agent loop normally intercepts ask_user and routes it to the channel's
        # clarify handler. This fallback only runs if no clarify handler is available.
        return "[ask_user is not available on this surface; proceed with the most reasonable assumption]"


class ForgetMemoryTool(BaseTool):
    """Remove a single matching bullet line from USER.md or MEMORY.md."""

    name = "forget_memory"
    description = (
        "Forget one stored fact: remove a single bullet line from USER.md or MEMORY.md "
        "that contains the given text. Refuses if zero or more than one line matches, so "
        "you never delete too much. Use when the user asks you to forget something."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "file": {
                "type": "string",
                "enum": ["USER.md", "MEMORY.md"],
                "description": "Which memory file to edit.",
            },
            "match": {
                "type": "string",
                "description": "Text contained in the bullet line to remove (must match exactly one line).",
            },
        },
        "required": ["file", "match"],
    }
    destructive = True  # routes through HITL approval; the owner sees file+match first

    def __init__(self, workspace_dir: str = "workspace/agent") -> None:
        self._workspace_dir = workspace_dir

    async def execute(self, **params: Any) -> str:
        file_name: str = params.get("file", "")
        match: str = (params.get("match", "") or "").strip()
        if file_name not in ("USER.md", "MEMORY.md"):
            return "Error: file must be 'USER.md' or 'MEMORY.md'."
        if not match:
            return "Error: match text is required."

        resolved = _safe_resolve(os.path.join(self._workspace_dir, file_name))
        if not os.path.isfile(resolved):
            return f"Error: {file_name} does not exist."

        try:
            async with aiofiles.open(resolved, mode="r", encoding="utf-8") as fh:
                content = await fh.read()
        except Exception as exc:  # noqa: BLE001
            logger.exception("ForgetMemoryTool read error")
            return f"Error reading {file_name}: {exc}"

        lines = content.splitlines()
        needle = match.lower()
        hits = [
            i for i, ln in enumerate(lines)
            if ln.lstrip().startswith(("- ", "* ")) and needle in ln.lower()
        ]

        if not hits:
            return f"No memory line matched '{match}'. Nothing removed."
        if len(hits) > 1:
            preview = "\n".join(f"  {n + 1}. {lines[n].strip()}" for n in hits)
            return (
                f"Refusing to delete: {len(hits)} lines matched '{match}'. "
                f"Make the text more specific so exactly one line matches:\n{preview}"
            )

        removed = lines.pop(hits[0])
        try:
            async with aiofiles.open(resolved, mode="w", encoding="utf-8") as fh:
                await fh.write("\n".join(lines) + ("\n" if content.endswith("\n") else ""))
        except Exception as exc:  # noqa: BLE001
            logger.exception("ForgetMemoryTool write error")
            return f"Error writing {file_name}: {exc}"
        return f"Removed from {file_name}: {removed.strip()}"


class SearchFilesTool(BaseTool):
    """Search workspace files by content (grep without shell)."""

    name = "search_files"
    description = (
        "Search files in the workspace for a text query (case-insensitive substring, or "
        "regex when regex=true). Returns file:line: snippet matches. Use to find where "
        "something is written without running shell grep."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Text to search for."},
            "path": {"type": "string", "description": "Subdirectory to search (default: workspace root)."},
            "regex": {"type": "boolean", "description": "Treat query as a regular expression. Default false."},
        },
        "required": ["query"],
    }
    destructive = False

    _MAX_RESULTS = 100
    _MAX_FILE_BYTES = 2 * 1024 * 1024  # skip files larger than 2 MB

    def __init__(self, workspace_dir: str | None = None) -> None:
        self._workspace_dir = workspace_dir

    async def execute(self, **params: Any) -> str:
        query: str = params.get("query", "")
        if not query:
            return "Error: query is required."
        path = params.get("path")
        use_regex = bool(params.get("regex", False))

        try:
            if path:
                root = _resolve_file_path(path, self._workspace_dir)
            else:
                # Default: the workspace root itself (NOT cwd), so search stays scoped.
                base = self._workspace_dir or "."
                root = _safe_resolve(base)
        except PermissionError as exc:
            return str(exc)
        if not os.path.isdir(root):
            return f"Error: '{root}' is not a directory."

        import re as _re

        if use_regex:
            try:
                pattern = _re.compile(query, _re.IGNORECASE)
            except _re.error as exc:
                return f"Error: invalid regex: {exc}"
            matcher = lambda line: pattern.search(line) is not None  # noqa: E731
        else:
            needle = query.lower()
            matcher = lambda line: needle in line.lower()  # noqa: E731

        results = await asyncio.to_thread(self._walk_and_match, root, matcher)
        if not results:
            return f"No matches for '{query}'."
        capped = results[: self._MAX_RESULTS]
        more = "" if len(results) <= self._MAX_RESULTS else f"\n… and {len(results) - self._MAX_RESULTS} more matches"
        return "\n".join(capped) + more

    def _walk_and_match(self, root: str, matcher: Any) -> list[str]:
        out: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"]
            for fname in sorted(filenames):
                if fname.startswith("."):
                    continue
                full = os.path.join(dirpath, fname)
                try:
                    if os.path.getsize(full) > self._MAX_FILE_BYTES:
                        continue
                    with open(full, "r", encoding="utf-8", errors="strict") as fh:
                        for lineno, line in enumerate(fh, 1):
                            if matcher(line):
                                rel = os.path.relpath(full, root)
                                snippet = line.strip()[:200]
                                out.append(f"{rel}:{lineno}: {snippet}")
                                if len(out) >= self._MAX_RESULTS + 1:
                                    return out
                except (OSError, UnicodeDecodeError):
                    continue  # skip binaries / unreadable files
        return out


def create_default_registry(
    workspace_dir: str = "workspace/agent",
    max_file_size_mb: int = _DEFAULT_MAX_FILE_SIZE_MB,
    allow_shell: bool = True,
    allow_file_edit: bool = True,
) -> ToolRegistry:
    """Create a :class:`ToolRegistry` pre-populated with the built-in tools.

    Respects the system config toggles for shell execution and file editing.
    """
    registry = ToolRegistry()

    if allow_shell:
        registry.register(ExecuteShellTool())
    if allow_file_edit:
        registry.register(ReadFileTool(max_file_size_mb=max_file_size_mb, workspace_dir=workspace_dir))
        registry.register(WriteFileTool(workspace_dir=workspace_dir))
        registry.register(ReplaceInFileTool(workspace_dir=workspace_dir))
        registry.register(ListFilesTool(workspace_dir=workspace_dir))
        registry.register(MakeDirectoryTool(workspace_dir=workspace_dir))
        registry.register(DeleteFileTool(workspace_dir=workspace_dir))

    registry.register(WebSearchTool())
    registry.register(FetchUrlTool())
    registry.register(GetCurrentTimeTool())
    registry.register(SearchFilesTool(workspace_dir=workspace_dir))
    registry.register(UpdateUserFactTool(workspace_dir=workspace_dir))
    registry.register(UpdateProjectMemoryTool(workspace_dir=workspace_dir))
    if allow_file_edit:
        registry.register(ForgetMemoryTool(workspace_dir=workspace_dir))
    registry.register(AskUserTool())

    return registry
