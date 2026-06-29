"""Lightweight asyncio job scheduler with natural-language-friendly schedules.

Jobs persist to a JSON file and are fired by a background loop. Each job runs the
agent with a stored prompt and the result is sent to the owner's chat. Scheduled
jobs run with a NON-destructive toolset (same pruning as sub-agents) because they
run unattended — there is no human to approve a destructive action.

Schedule types:
- interval : every N minutes
- daily    : at HH:MM (local time)
- once     : at an ISO datetime, then auto-disables

The due-time logic is pure and unit-tested; the run loop / agent firing are not.
Run the self-check:  python3 -m core.scheduler
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from core.tools import BaseTool, ToolRegistry

logger = logging.getLogger(__name__)

MAX_JOBS = 50
_CHECK_INTERVAL_SECONDS = 30


def is_due(job: dict[str, Any], now: datetime) -> bool:
    """Pure: is *job* due to run at *now*? (no side effects)"""
    if not job.get("enabled", True):
        return False
    last = job.get("last_run")
    last_dt = datetime.fromisoformat(last) if last else None
    stype = job.get("type")

    if stype == "interval":
        mins = int(job.get("interval_minutes", 0) or 0)
        if mins <= 0:
            return False
        return last_dt is None or (now - last_dt) >= timedelta(minutes=mins)

    if stype == "daily":
        hour = int(job.get("hour", 0) or 0)
        minute = int(job.get("minute", 0) or 0)
        if (now.hour, now.minute) < (hour, minute):
            return False  # not yet time today
        return last_dt is None or last_dt.date() < now.date()

    if stype == "once":
        run_at = job.get("run_at")
        if not run_at or job.get("fired"):
            return False
        return now >= datetime.fromisoformat(run_at)

    return False


def describe(job: dict[str, Any]) -> str:
    """Human-readable one-liner for a job."""
    stype = job.get("type")
    if stype == "interval":
        when = f"every {job.get('interval_minutes')} min"
    elif stype == "daily":
        when = f"daily at {int(job.get('hour', 0)):02d}:{int(job.get('minute', 0)):02d}"
    elif stype == "once":
        when = f"once at {job.get('run_at')}"
    else:
        when = "?"
    state = "" if job.get("enabled", True) else " (disabled)"
    return f"[{job.get('id')}] {job.get('name', 'unnamed')} — {when}{state}"


class Scheduler:
    """Persisted job list + a background loop that fires due jobs."""

    def __init__(self, store_path: str | Path) -> None:
        self.path = Path(store_path)
        self.jobs: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self.jobs = json.loads(self.path.read_text(encoding="utf-8")) or []
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not load schedules: %s", exc)
                self.jobs = []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.jobs, indent=2, ensure_ascii=False), encoding="utf-8")

    def add(self, job: dict[str, Any]) -> dict[str, Any]:
        if len(self.jobs) >= MAX_JOBS:
            raise ValueError(f"Job limit reached ({MAX_JOBS}). Cancel some first.")
        job.setdefault("id", uuid.uuid4().hex[:8])
        job.setdefault("enabled", True)
        self.jobs.append(job)
        self._save()
        return job

    def remove(self, job_id: str) -> bool:
        before = len(self.jobs)
        self.jobs = [j for j in self.jobs if j.get("id") != job_id]
        if len(self.jobs) != before:
            self._save()
            return True
        return False

    def list(self) -> list[dict[str, Any]]:
        return list(self.jobs)

    def mark_ran(self, job: dict[str, Any], now: datetime) -> None:
        job["last_run"] = now.isoformat()
        if job.get("type") == "once":
            job["fired"] = True
            job["enabled"] = False
        self._save()

    async def run_forever(self, fire: Callable[[dict[str, Any]], Awaitable[None]], now_fn: Callable[[], datetime] = datetime.now) -> None:
        """Check every ~30s and fire due jobs. Never raises out of the loop."""
        logger.info("Scheduler started (%d job(s)).", len(self.jobs))
        while True:
            try:
                now = now_fn()
                for job in list(self.jobs):
                    if is_due(job, now):
                        logger.info("Firing scheduled job %s (%s).", job.get("id"), job.get("name"))
                        try:
                            await fire(job)
                        except Exception as exc:  # noqa: BLE001
                            logger.exception("Scheduled job %s failed: %s", job.get("id"), exc)
                        self.mark_ran(job, now)
            except Exception:  # noqa: BLE001
                logger.exception("Scheduler loop error")
            await asyncio.sleep(_CHECK_INTERVAL_SECONDS)


_SCHEDULE_PARAMS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Short name for the task."},
        "prompt": {"type": "string", "description": "What the agent should do when it fires (it runs this as a message)."},
        "schedule_type": {"type": "string", "enum": ["interval", "daily", "once"]},
        "interval_minutes": {"type": "integer", "description": "For schedule_type=interval: minutes between runs."},
        "hour": {"type": "integer", "description": "For schedule_type=daily: hour 0-23 (local)."},
        "minute": {"type": "integer", "description": "For schedule_type=daily: minute 0-59."},
        "run_at": {"type": "string", "description": "For schedule_type=once: ISO datetime, e.g. 2026-06-28T17:00."},
    },
    "required": ["name", "prompt", "schedule_type"],
}


class ScheduleTaskTool(BaseTool):
    name = "schedule_task"
    description = (
        "Schedule a task to run later and report back to the user (reminders, daily "
        "briefings, monitoring). Translate the user's natural-language time into the "
        "fields: interval (every N minutes), daily (hour+minute), or once (run_at ISO)."
    )
    parameters = _SCHEDULE_PARAMS
    destructive = False

    def __init__(self, scheduler: Scheduler, chat_id: int) -> None:
        self._sched = scheduler
        self._chat_id = chat_id

    async def execute(self, **params: Any) -> str:
        stype = params.get("schedule_type")
        if stype not in ("interval", "daily", "once"):
            return "Error: schedule_type must be interval, daily, or once."
        job: dict[str, Any] = {
            "name": str(params.get("name", "")).strip() or "task",
            "prompt": str(params.get("prompt", "")).strip(),
            "type": stype,
            "chat_id": self._chat_id,
        }
        if not job["prompt"]:
            return "Error: prompt is required."
        if stype == "interval":
            job["interval_minutes"] = int(params.get("interval_minutes", 0) or 0)
            if job["interval_minutes"] <= 0:
                return "Error: interval_minutes must be > 0."
        elif stype == "daily":
            job["hour"] = max(0, min(23, int(params.get("hour", 0) or 0)))
            job["minute"] = max(0, min(59, int(params.get("minute", 0) or 0)))
        elif stype == "once":
            run_at = str(params.get("run_at", "")).strip()
            try:
                datetime.fromisoformat(run_at)
            except ValueError:
                return "Error: run_at must be an ISO datetime like 2026-06-28T17:00."
            job["run_at"] = run_at
        try:
            saved = self._sched.add(job)
        except ValueError as exc:
            return f"Error: {exc}"
        return f"Scheduled ✅ {describe(saved)}"


class ListSchedulesTool(BaseTool):
    name = "list_schedules"
    description = "List all scheduled tasks."
    parameters = {"type": "object", "properties": {}}
    destructive = False

    def __init__(self, scheduler: Scheduler, chat_id: int) -> None:
        self._sched = scheduler

    async def execute(self, **params: Any) -> str:
        jobs = self._sched.list()
        if not jobs:
            return "No scheduled tasks."
        return "Scheduled tasks:\n" + "\n".join(describe(j) for j in jobs)


class CancelScheduleTool(BaseTool):
    name = "cancel_schedule"
    description = "Cancel a scheduled task by its id (see list_schedules)."
    parameters = {
        "type": "object",
        "properties": {"id": {"type": "string", "description": "The job id to cancel."}},
        "required": ["id"],
    }
    destructive = False

    def __init__(self, scheduler: Scheduler, chat_id: int) -> None:
        self._sched = scheduler

    async def execute(self, **params: Any) -> str:
        job_id = str(params.get("id", "")).strip()
        return f"Cancelled: {job_id}" if self._sched.remove(job_id) else f"No task with id {job_id}."


def attach_schedule_tools(registry: ToolRegistry, scheduler: Scheduler, chat_id: int) -> ToolRegistry:
    """Return a clone of *registry* with the per-chat scheduling tools added."""
    cloned = registry.clone()
    cloned.register(ScheduleTaskTool(scheduler, chat_id))
    cloned.register(ListSchedulesTool(scheduler, chat_id))
    cloned.register(CancelScheduleTool(scheduler, chat_id))
    return cloned


def _self_check() -> None:
    d = datetime
    # interval: due when never run, and after the interval elapses
    j = {"type": "interval", "interval_minutes": 30, "enabled": True}
    assert is_due(j, d(2026, 6, 27, 12, 0))
    j["last_run"] = d(2026, 6, 27, 12, 0).isoformat()
    assert not is_due(j, d(2026, 6, 27, 12, 20))
    assert is_due(j, d(2026, 6, 27, 12, 31))

    # daily: due once past HH:MM, not before, not twice the same day
    day = {"type": "daily", "hour": 17, "minute": 0, "enabled": True}
    assert not is_due(day, d(2026, 6, 27, 16, 59))
    assert is_due(day, d(2026, 6, 27, 17, 0))
    day["last_run"] = d(2026, 6, 27, 17, 0).isoformat()
    assert not is_due(day, d(2026, 6, 27, 18, 0))
    assert is_due(day, d(2026, 6, 28, 17, 0))

    # once: due at/after run_at, then 'fired' stops it
    once = {"type": "once", "run_at": d(2026, 6, 27, 17, 0).isoformat(), "enabled": True}
    assert not is_due(once, d(2026, 6, 27, 16, 0))
    assert is_due(once, d(2026, 6, 27, 17, 1))
    once["fired"] = True
    assert not is_due(once, d(2026, 6, 27, 18, 0))

    # disabled never fires
    assert not is_due({"type": "interval", "interval_minutes": 1, "enabled": False}, d(2026, 6, 27, 12, 0))

    print("scheduler self-check: OK")


if __name__ == "__main__":
    _self_check()
