"""Permission policy for sub-agents: which tools a spawned sub-agent may use.

A sub-agent gets only NON-destructive tools and may NOT spawn further sub-agents
(prevents unbounded recursion and runaway token cost). Pure stdlib so the
permission reduction — the security-critical part — is unit-testable.

Run the self-check directly:  python3 core/subagent_policy.py
"""

from __future__ import annotations

from collections.abc import Iterable

SUBAGENT_TOOL_NAME = "spawn_subagent"
# A sub-agent is autonomous: it can't spawn another sub-agent, and it has no
# channel to ask the user, so ask_user is excluded too.
_EXCLUDED = frozenset({SUBAGENT_TOOL_NAME, "ask_user"})


def is_safe_for_subagent(name: str, destructive: bool) -> bool:
    """Safe iff the tool is non-destructive AND is not the subagent spawner."""
    return (not destructive) and (name not in _EXCLUDED)


def select_subagent_tool_names(tools: Iterable[tuple[str, bool]]) -> list[str]:
    """Filter (name, destructive) pairs down to those safe for a sub-agent."""
    return [name for name, destructive in tools if is_safe_for_subagent(name, destructive)]


def _self_check() -> None:
    sample = [
        ("read_file", False),
        ("web_search", False),
        ("update_user_fact", False),
        ("update_project_memory", False),
        ("execute_shell", True),      # destructive → excluded
        ("write_file", True),         # destructive → excluded
        ("replace_in_file", True),    # destructive → excluded
        ("spawn_subagent", False),    # excluded → no recursion
    ]
    keep = select_subagent_tool_names(sample)
    assert keep == ["read_file", "web_search", "update_user_fact", "update_project_memory"], keep
    assert is_safe_for_subagent("read_file", False)
    assert not is_safe_for_subagent("execute_shell", True)
    assert not is_safe_for_subagent("spawn_subagent", False)
    assert not is_safe_for_subagent("ask_user", False)  # autonomous: no user to ask
    print("subagent_policy self-check: OK")


if __name__ == "__main__":
    _self_check()
