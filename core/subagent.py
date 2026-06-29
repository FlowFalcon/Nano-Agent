"""spawn_subagent tool: delegate a focused subtask to a sub-agent.

The sub-agent runs the same agent loop with a reduced, non-destructive toolset
and no human-approval channel. Because its registry is pruned to non-destructive
tools and excludes spawn_subagent itself (see core.subagent_policy), it can
neither run destructive tools nor spawn further sub-agents.
"""

from __future__ import annotations

import logging
from typing import Any

from config.settings import AppConfig
from core.agent import run_agent_once
from core.memory import WorkspaceMemory
from core.subagent_policy import is_safe_for_subagent
from core.tools import BaseTool, ToolRegistry

logger = logging.getLogger(__name__)


def prune_registry_for_subagent(base: ToolRegistry) -> ToolRegistry:
    """Return a NEW registry containing only tools safe for a sub-agent."""
    pruned = ToolRegistry()
    for name in base.list_names():
        tool = base.get(name)
        if tool is not None and is_safe_for_subagent(tool.name, bool(getattr(tool, "destructive", False))):
            pruned.register(tool)
    return pruned


class SubagentTool(BaseTool):
    """Run a focused subtask through the agent loop with a restricted toolset."""

    name = "spawn_subagent"
    description = (
        "Delegate a focused, self-contained subtask to a sub-agent. The sub-agent "
        "has non-destructive tools only (no shell, no arbitrary file writes; workspace "
        "memory tools are allowed) and cannot spawn further sub-agents. Returns the "
        "sub-agent's final answer as text."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The subtask for the sub-agent to complete. Must be self-contained.",
            },
        },
        "required": ["task"],
    }
    destructive = False

    def __init__(self, memory: WorkspaceMemory, config: AppConfig, base_registry: ToolRegistry) -> None:
        self._memory = memory
        self._config = config
        self._base = base_registry

    async def execute(self, **params: Any) -> str:
        task = str(params.get("task", "")).strip()
        if not task:
            return "Error: task is required."

        sub_registry = prune_registry_for_subagent(self._base)
        try:
            return await run_agent_once(
                task,
                tools=sub_registry,
                memory=self._memory,
                config=self._config,
                include_private_memory=False,
                allow_tools=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Subagent execution failed")
            return f"Subagent error: {exc}"
