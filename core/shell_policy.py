"""Shell command allowlist policy — pure, dependency-free, independently testable.

Decides whether an ``execute_shell`` command may run WITHOUT human approval under
``exec_approval_mode='list'``. Kept import-light (stdlib only) on purpose so the
security-critical matching can be unit-tested without the agent's heavy deps.

Run the self-check directly:  ``python3 core/shell_policy.py``
"""

from __future__ import annotations

import fnmatch
import shlex

# Characters that let a shell chain, substitute, or redirect commands. When any of
# these appear, executable-name and glob-prefix allowlist matches are unsafe — e.g.
# `ls && rm -rf /` must not ride in on a benign `ls` entry — so we require an exact
# allowlist match instead. (`*?[` are intentionally NOT here: those are filename
# globbing, and are also the glob syntax used by allowlist entries themselves.)
_SHELL_CONTROL_CHARS: tuple[str, ...] = (";", "&", "|", "`", "$", "(", ")", "<", ">", "\n", "\r")


def normalize_command(command: str) -> str:
    """Collapse surrounding/duplicate whitespace for stable policy comparison."""
    return " ".join(command.strip().split())


def extract_executable(command: str) -> str:
    """Best-effort first executable token of a shell command."""
    try:
        parts = shlex.split(command, posix=True)
    except ValueError:
        parts = command.strip().split()
    return parts[0] if parts else ""


def has_shell_control(command: str) -> bool:
    """True if *command* contains shell chaining/substitution/redirection chars."""
    return any(ch in command for ch in _SHELL_CONTROL_CHARS)


def is_command_blocked(command: str, blocklist: list[str]) -> bool:
    """Return True if *command* matches a blocklist entry.

    Blocklist matching is deliberately AGGRESSIVE (safety over convenience): a
    bare-name entry like ``rm`` blocks the command if ``rm`` appears as ANY token
    (so ``ls && rm -rf /`` is blocked, not just a command that starts with rm).
    Blocked commands always require human approval regardless of exec mode.
    """
    normalized = normalize_command(command)
    try:
        tokens = set(shlex.split(normalized, posix=True))
    except ValueError:
        tokens = set(normalized.split())

    for raw_entry in blocklist:
        entry = normalize_command(str(raw_entry))
        if not entry:
            continue
        if any(ch in entry for ch in "*?[") and fnmatch.fnmatchcase(normalized, entry):
            return True
        if normalized == entry:
            return True
        if " " not in entry and entry in tokens:
            return True
        if " " in entry and entry in normalized:
            return True
    return False


def is_command_allowlisted(command: str, allowlist: list[str]) -> bool:
    """Return True when *command* may run without approval.

    Entry forms:
    - exact full command, e.g. ``python --version``
    - bare executable name, e.g. ``ls`` matches ``ls -la``
    - shell-style glob, e.g. ``python *`` or ``git status*``

    Security: when the command contains shell control operators, ONLY an exact
    allowlist match is honored. Executable-name and glob matches are skipped so a
    chained/substituted command (``ls && rm -rf /``) cannot ride in on ``ls``.
    """
    normalized = normalize_command(command)
    executable = extract_executable(normalized)
    dangerous = has_shell_control(normalized)

    for raw_entry in allowlist:
        entry = normalize_command(str(raw_entry))
        if not entry:
            continue

        # Exact match always wins — the user explicitly allowlisted this string,
        # operators and all.
        if normalized == entry:
            return True

        # With shell operators present, refuse name/glob shortcuts → force approval.
        if dangerous:
            continue

        has_glob = any(ch in entry for ch in "*?[")
        if has_glob and fnmatch.fnmatchcase(normalized, entry):
            return True

        # A single-token entry means "allow this executable with any args".
        if " " not in entry and executable == entry:
            return True

    return False


def _self_check() -> None:
    """Assert-based self-check. The bypass cases below are the whole point."""
    # Bare-executable entry: benign args allowed, chained/redirected commands denied.
    ls = ["ls"]
    assert is_command_allowlisted("ls", ls)
    assert is_command_allowlisted("ls -la", ls)
    assert is_command_allowlisted("  ls   -la  ", ls)  # whitespace-normalized
    assert not is_command_allowlisted("ls && rm -rf /", ls)   # chaining
    assert not is_command_allowlisted("ls; rm -rf /", ls)     # sequencing
    assert not is_command_allowlisted("ls | sh", ls)          # pipe
    assert not is_command_allowlisted("ls > /etc/passwd", ls)  # redirect
    assert not is_command_allowlisted("ls $(rm -rf /)", ls)   # substitution
    assert not is_command_allowlisted("ls `rm -rf /`", ls)    # backtick sub

    # Glob entry: prefix glob must not become a chaining bypass either.
    py = ["python *"]
    assert is_command_allowlisted("python script.py", py)
    assert not is_command_allowlisted("python x.py; rm -rf /", py)
    assert not is_command_allowlisted("python -c \"import os\"; rm -rf /", py)

    # Multi-token exact entry: only the exact command, nothing else.
    gs = ["git status"]
    assert is_command_allowlisted("git status", gs)
    assert not is_command_allowlisted("git push --force", gs)

    # Exact entry WITH operators: user explicitly allowlisted it → allowed exactly.
    chained = ["make build && make test"]
    assert is_command_allowlisted("make build && make test", chained)
    assert not is_command_allowlisted("make build && make test && rm -rf /", chained)

    # Empty / blank allowlist denies everything.
    assert not is_command_allowlisted("ls", [])
    assert not is_command_allowlisted("ls", ["", "   "])

    # Blocklist: aggressive — blocked exe matches as any token.
    block = ["rm", "shutdown", "git push*"]
    assert is_command_blocked("rm -rf /", block)
    assert is_command_blocked("ls && rm -rf /", block)        # rm hidden after &&
    assert is_command_blocked("shutdown now", block)
    assert is_command_blocked("git push --force", block)      # glob entry
    assert not is_command_blocked("ls -la", block)
    assert not is_command_blocked("remove_file.py", block)    # 'rm' not a standalone token
    assert not is_command_blocked("ls", [])

    print("shell_policy self-check: OK")


if __name__ == "__main__":
    _self_check()
