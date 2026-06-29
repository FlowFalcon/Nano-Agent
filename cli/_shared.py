"""Pure helpers shared by both setup wizards (Rich dashboard + plain panel).

These were duplicated in cli/dashboard.py and cli/panel_wizard.py. They are
stdlib-only and side-effect-free, so they live here and are unit-testable. The
two wizards keep their own I/O frontends (Rich/questionary vs plain input) and
import these.

Run the self-check directly:  python3 cli/_shared.py
"""

from __future__ import annotations

from typing import Any


def parse_user_ids(raw: str) -> list[int]:
    """Parse a comma-separated string of integer user IDs.

    Raises ValueError on the first non-integer entry.
    """
    ids: list[int] = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        try:
            ids.append(int(item))
        except ValueError as exc:
            raise ValueError(f"'{item}' is not a valid integer user ID") from exc
    return ids


def deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *overrides* into *base*, returning a NEW dict."""
    merged = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def mask_secret(value: str, visible: int = 8) -> str:
    """Return *value* with only the first *visible* chars shown."""
    if not value:
        return ""
    if len(value) <= visible:
        return value
    return value[:visible] + "..."


_SECRET_ENV_HINTS = ("key", "secret", "token", "password")


def mask_config_for_display(config: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of *config* with secrets masked for display.

    Masks telegram.bot_token, every llm provider api_key, and any mcp server
    env value whose key looks secret. Input is never mutated.
    """
    import copy

    masked = copy.deepcopy(config)

    telegram = masked.get("telegram")
    if isinstance(telegram, dict) and telegram.get("bot_token"):
        telegram["bot_token"] = mask_secret(telegram["bot_token"])

    llm = masked.get("llm")
    for provider in (llm.get("providers", []) if isinstance(llm, dict) else []):
        if isinstance(provider, dict) and provider.get("api_key"):
            provider["api_key"] = mask_secret(provider["api_key"])

    mcp = masked.get("mcp")
    for server in (mcp.get("servers", []) if isinstance(mcp, dict) else []):
        env = server.get("env") if isinstance(server, dict) else None
        if isinstance(env, dict):
            for env_key, env_val in env.items():
                if any(hint in env_key.lower() for hint in _SECRET_ENV_HINTS):
                    env[env_key] = mask_secret(env_val)

    return masked


def install_playwright(printer=print, *, runtime=None) -> bool:
    """Environment-aware browser setup for Playwright + Chromium.

    Installs on real servers (VPS/container/local) and skips on hosts that cannot
    run a browser (Pterodactyl panels, serverless). On root hosts it also pulls the
    OS libraries Chromium needs. Verifies the result before reporting success.
    Returns True only when a usable browser is in place. Never raises."""
    import subprocess
    import sys

    from core.runtime import detect_runtime_environment

    rt = runtime or detect_runtime_environment()

    if rt.is_serverless:
        printer(
            "Serverless host detected — skipping browser install. Browsers need a "
            "persistent, writable filesystem; run on a VPS/container for browser features."
        )
        return False
    if rt.is_pterodactyl:
        printer(
            "Pterodactyl panel detected — skipping automatic browser install. Panels "
            "usually lack the system libraries Chromium needs and cannot run apt. If your "
            "egg supports it: pip install playwright && playwright install chromium"
        )
        return False

    printer("Installing Playwright (pip wheel)…")
    try:
        wheel = subprocess.run(
            [sys.executable, "-m", "pip", "install", "playwright"],
            capture_output=True, text=True, timeout=300,
        )
    except Exception as exc:  # noqa: BLE001
        printer(f"Could not install Playwright: {exc}. Run later: pip install playwright")
        return False
    if wheel.returncode != 0:
        printer(f"Playwright wheel install failed (exit {wheel.returncode}). Run later: pip install playwright")
        return False

    cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
    if rt.is_root:
        cmd.append("--with-deps")
    printer(f"Downloading Chromium{' + system deps' if rt.is_root else ''}… (this can take a minute)")
    try:
        browser = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except Exception as exc:  # noqa: BLE001
        printer(f"Chromium download failed: {exc}. Run later: playwright install chromium")
        return False
    if browser.returncode != 0:
        tail = (browser.stderr or browser.stdout or "").strip().splitlines()[-1:]
        hint = f" ({tail[0]})" if tail else ""
        printer(
            f"Chromium install failed (exit {browser.returncode}){hint}. "
            "On a minimal host, run: playwright install --with-deps chromium"
        )
        return False

    return _verify_chromium(printer)


def _verify_chromium(printer=print) -> bool:
    """Confirm Chromium is actually present and registered, not just downloaded."""
    import importlib
    import os

    importlib.invalidate_caches()
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        printer(f"Playwright import failed after install: {exc}")
        return False
    try:
        with sync_playwright() as p:
            path = p.chromium.executable_path
    except Exception as exc:  # noqa: BLE001
        printer(f"Could not verify Chromium: {exc}")
        return False
    if path and os.path.exists(path):
        printer("Browser ready ✓ (Chromium verified).")
        return True
    printer("Chromium not found after install. Run later: playwright install chromium")
    return False


def _self_check() -> None:
    assert parse_user_ids("1, 2 ,3") == [1, 2, 3]
    assert parse_user_ids("  ") == []
    assert parse_user_ids("") == []
    try:
        parse_user_ids("1, x")
    except ValueError as exc:
        assert "not a valid" in str(exc)
    else:
        raise AssertionError("expected ValueError on non-integer id")

    # deep_merge: nested merge, override wins, input not mutated.
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    out = deep_merge(base, {"a": {"y": 9, "z": 4}, "c": 5})
    assert out == {"a": {"x": 1, "y": 9, "z": 4}, "b": 3, "c": 5}, out
    assert base == {"a": {"x": 1, "y": 2}, "b": 3}, "input must not be mutated"
    # non-dict override replaces a dict.
    assert deep_merge({"a": {"x": 1}}, {"a": 7}) == {"a": 7}

    assert mask_secret("") == ""
    assert mask_secret("short") == "short"
    assert mask_secret("0123456789abc") == "01234567..."

    # mask_config_for_display: all secret surfaces masked, non-secrets + input untouched.
    cfg = {
        "telegram": {"bot_token": "123456789:ABCDEFGHIJ"},
        "llm": {"providers": [{"api_key": "sk-secretkey1234"}]},
        "mcp": {"servers": [{"env": {"API_TOKEN": "tok_abcdef123", "REGION": "us"}}]},
    }
    m = mask_config_for_display(cfg)
    assert m["telegram"]["bot_token"] == "12345678..."
    assert m["llm"]["providers"][0]["api_key"] == "sk-secre..."
    assert m["mcp"]["servers"][0]["env"]["API_TOKEN"] == "tok_abcd..."
    assert m["mcp"]["servers"][0]["env"]["REGION"] == "us"
    assert cfg["telegram"]["bot_token"] == "123456789:ABCDEFGHIJ", "input must not be mutated"

    print("cli/_shared self-check: OK")


if __name__ == "__main__":
    _self_check()
