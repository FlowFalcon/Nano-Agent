"""Lightweight process manager for bare-metal runs: start / stop / status / doctor.

ponytail: for production, prefer Docker (`restart: unless-stopped`, already in
docker-compose.yml) or systemd — they supervise and restart on crash. This is a
convenience for running directly with `python3 main.py` on a host without a
supervisor. PID file + SIGTERM only; no daemonization.

Usage:
    python3 cli/manage.py start | stop | status | doctor | selfcheck
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Allow running directly as `python3 cli/manage.py` (put project root on sys.path
# so `doctor` can `import config`). Harmless when run as `python3 -m cli.manage`.
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_PID_FILE = Path(os.environ.get("AGENT_PID_FILE", ".agent.pid"))
_MAIN = _PROJECT_ROOT / "main.py"
_STOP_WAIT_TICKS = 20  # 20 * 0.25s = 5s grace before giving up on a clean stop


def read_pid(path: Path) -> int | None:
    """Read a PID from *path*, or None if missing/garbage."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def is_running(pid: int) -> bool:
    """True if a process with *pid* exists (signal 0 probe)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    return True


def _running_pid(path: Path) -> int | None:
    pid = read_pid(path)
    return pid if (pid is not None and is_running(pid)) else None


def cmd_start(_args: argparse.Namespace) -> int:
    existing = _running_pid(_PID_FILE)
    if existing:
        print(f"Already running (pid {existing}).")
        return 0
    proc = subprocess.Popen([sys.executable, str(_MAIN)])  # noqa: S603
    _PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    print(f"Started (pid {proc.pid}).")
    return 0


def cmd_stop(_args: argparse.Namespace) -> int:
    pid = _running_pid(_PID_FILE)
    if not pid:
        print("Not running.")
        _PID_FILE.unlink(missing_ok=True)
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(_STOP_WAIT_TICKS):
        if not is_running(pid):
            break
        time.sleep(0.25)
    _PID_FILE.unlink(missing_ok=True)
    print(f"Stopped (pid {pid}).")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    pid = _running_pid(_PID_FILE)
    print(f"running (pid {pid})" if pid else "stopped")
    return 0 if pid else 1


def cmd_doctor(_args: argparse.Namespace) -> int:
    """Validate config and secrets without starting the bot."""
    try:
        from config.settings import load_config
        config = load_config()
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] config: {exc}")
        return 1
    print("[ok]   config loads & validates")

    ok = True
    if not config.telegram.bot_token:
        print("[FAIL] telegram.bot_token empty (set in config.json or TELEGRAM_BOT_TOKEN)")
        ok = False
    else:
        print("[ok]   telegram bot_token present")

    missing = [p.name for p in config.llm.providers if not p.api_key]
    if missing:
        print(f"[FAIL] LLM providers missing api_key: {missing}")
        ok = False
    else:
        print(f"[ok]   {len(config.llm.providers)} LLM provider(s) have api_key")

    print("doctor: " + ("all basic checks passed." if ok else "problems found above."))
    print("(Provider connectivity is verified at runtime via fallback.)")
    return 0 if ok else 1


def _self_check(_args: argparse.Namespace | None = None) -> int:
    assert read_pid(Path("/nonexistent/path/.agent.pid")) is None
    assert is_running(os.getpid()) is True
    assert is_running(2_000_000_000) is False     # almost certainly not a live pid
    assert is_running(0) is False
    print("manage self-check: OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="manage", description="Run/inspect the agent process.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    handlers = {
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "doctor": cmd_doctor,
        "selfcheck": _self_check,
    }
    for name, fn in handlers.items():
        sub.add_parser(name).set_defaults(fn=fn)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
