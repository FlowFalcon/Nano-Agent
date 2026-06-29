"""Line-based first-run wizard for panel consoles such as Pterodactyl.

Unlike the Rich dashboard wizard, this module intentionally uses plain
``print``/``input``.  Panel consoles often do not behave like full terminals,
so prompts are simple: users answer with letters or numbers.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

# Allow running directly as `python3 cli/panel_wizard.py`.
path = Path(__file__).resolve().parent.parent
if str(path) not in sys.path:
    sys.path.insert(0, str(path))

from rich.console import Console

from config.settings import AppConfig, get_default_config_template, resolve_config_path
from cli._shared import deep_merge, mask_config_for_display, parse_user_ids as _parse_user_ids
from config.version import __project__, __version__

# Rich output (banners/headers) renders fine on panel consoles; only the INPUT
# stays plain (input()) because panels don't support questionary's arrow keys.
_console = Console()


def _banner() -> None:
    _console.clear()
    _console.print(f"[bold bright_cyan]{__project__}[/bold bright_cyan] [dim]v{__version__} · setup (panel mode)[/dim]")
    _console.print("  [dim]Answer with a letter or number, e.g. A / B / 1 / 2.[/dim]")
    _console.print("  [yellow]Note:[/yellow] [dim]in a panel console, tokens may appear in the panel log —[/dim]")
    _console.print("  [dim]      fill config.json manually or via env secrets for more safety.[/dim]")
    _console.print()


def _prompt(text: str, default: str | None = None) -> str:
    print(text)
    if default not in (None, ""):
        print(f"Default: {default}")
    print()
    suffix = f" [{default}]" if default not in (None, "") else ""
    try:
        value = input(f">>{suffix} ").strip()
    except EOFError:
        print("\nCannot read input from console. Create config.json manually.", file=sys.stderr)
        raise SystemExit(1)
    if not value and default is not None:
        return default
    return value


def _choice(text: str, options: list[tuple[str, str]], default: str | None = None) -> str:
    """Prompt a letter/number menu and return the option key."""

    normalized: dict[str, str] = {}
    print(text)
    print()
    for idx, (key, label) in enumerate(options, start=1):
        upper = key.upper()
        print(f"[{upper}] {label}")
        normalized[upper] = upper
        normalized[key.lower()] = upper
        normalized[str(idx)] = upper
        if label.lower().startswith("yes"):
            normalized["y"] = upper
            normalized["yes"] = upper
        if label.lower().startswith("no"):
            normalized["n"] = upper
            normalized["no"] = upper
    if default:
        print(f"Default: {default.upper()}")
    print()

    while True:
        try:
            raw = input(">> ").strip()
        except EOFError:
            print("\nCannot read input from console. Create config.json manually.", file=sys.stderr)
            raise SystemExit(1)
        if not raw and default:
            return default.upper()
        if raw in normalized:
            return normalized[raw]
        if raw.upper() in normalized:
            return normalized[raw.upper()]
        print("Invalid choice. Try again.")
        print()


def _confirm(text: str, default: bool = True) -> bool:
    default_key = "A" if default else "B"
    choice = _choice(text, [("A", "yes"), ("B", "no")], default=default_key)
    return choice == "A"


def _load_existing_or_template(config_path: Path) -> dict[str, Any] | None:
    if not config_path.exists():
        return get_default_config_template()

    choice = _choice(
        f"Config already exists at {config_path.resolve()}. What do you want to do?",
        [
            ("A", "update - keep existing values as defaults"),
            ("B", "overwrite - start from a blank template"),
            ("C", "cancel"),
        ],
        default="A",
    )
    if choice == "C":
        return None
    if choice == "B":
        return get_default_config_template()

    try:
        existing = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Could not read existing config: {exc}")
        print("Using a fresh template.")
        return get_default_config_template()

    return deep_merge(get_default_config_template(), existing)


def _collect_basics(config: dict[str, Any]) -> None:
    _console.print("\n[bold cyan]▸ Step 1/5[/bold cyan]  [bold]Agent Identity[/bold]")
    config.setdefault("system", {})
    name = _prompt("What should we name your assistant?", config["system"].get("agent_name", "Nano-Agent"))
    config["system"]["agent_name"] = name.strip() or "Nano-Agent"


def _collect_telegram(config: dict[str, Any]) -> None:
    _console.print("\n[bold cyan]▸ Step 2/5[/bold cyan]  [bold]Telegram Setup[/bold]")
    while True:
        token = _prompt("Enter the Telegram Bot Token from @BotFather.", config["telegram"].get("bot_token") or None)
        parts = token.strip().split(":")
        if len(parts) == 2 and parts[0].isdigit():
            config["telegram"]["bot_token"] = token.strip()
            break
        print("Invalid token format. Example: 123456:ABCDEF")
        print()

    while True:
        raw_ids = _prompt(
            "Enter the owner allowed_user_ids. Separate with commas if more than one.",
            ",".join(str(x) for x in config["telegram"].get("allowed_user_ids", [])) or None,
        )
        try:
            ids = _parse_user_ids(raw_ids)
            if not ids:
                raise ValueError("at least one user ID is required")
            config["telegram"]["allowed_user_ids"] = ids
            break
        except ValueError as exc:
            print(f"Invalid user ID input: {exc}")
            print()

    config["telegram"]["enable_private_draft_streaming"] = _confirm(
        "Enable draft streaming for the owner's private chat?",
        default=bool(config["telegram"].get("enable_private_draft_streaming", True)),
    )

    mode_choice = _choice(
        "Choose the Telegram bot mode.",
        [("A", "polling"), ("B", "webhook")],
        default="A" if config["telegram"].get("mode", "polling") == "polling" else "B",
    )
    config["telegram"]["mode"] = "polling" if mode_choice == "A" else "webhook"

    if config["telegram"]["mode"] == "webhook":
        config["telegram"]["webhook_host"] = _prompt("Webhook bind host.", config["telegram"].get("webhook_host", "0.0.0.0"))
        while True:
            try:
                port = int(_prompt("Webhook bind port.", str(config["telegram"].get("webhook_port", 8080))))
                if not (1 <= port <= 65535):
                    raise ValueError
                config["telegram"]["webhook_port"] = port
                break
            except ValueError:
                print("Invalid port. Enter a number between 1-65535.")
        config["telegram"]["webhook_path"] = _prompt("Webhook path.", config["telegram"].get("webhook_path", "/webhook"))
        config["telegram"]["webhook_url"] = _prompt("Webhook public base URL.", config["telegram"].get("webhook_url", ""))


def _collect_llm(config: dict[str, Any]) -> None:
    _console.print("\n[bold cyan]▸ Step 3/5[/bold cyan]  [bold]LLM Provider[/bold]")
    existing = (config.get("llm", {}).get("providers") or [{}])[0]
    provider = {
        "priority": 1,
        "name": _prompt("Provider name.", existing.get("name", "openrouter")),
        "base_url": _prompt(
            "Chat completions base URL.",
            existing.get("base_url", "https://openrouter.ai/api/v1/chat/completions"),
        ),
        "api_key": _prompt("API key.", existing.get("api_key") or None),
        "model": _prompt("Model name.", existing.get("model", "anthropic/claude-sonnet-4-20250514")),
    }
    config["llm"]["providers"] = [provider]


def _collect_mcp(config: dict[str, Any]) -> None:
    _console.print("\n[bold cyan]▸ Step 4/5[/bold cyan]  [bold]MCP Servers[/bold] [dim](optional)[/dim]")
    if _confirm("Configure MCP (Model Context Protocol) servers?", default=False):
        print("MCP configuration via panel is not fully supported yet.")
        print("Please edit config.json manually for MCP.")
    config.setdefault("mcp", {"servers": []})


def _collect_system(config: dict[str, Any]) -> None:
    _console.print("\n[bold cyan]▸ Step 5/5[/bold cyan]  [bold]System Settings[/bold]")
    config["system"]["allow_shell_exec"] = _confirm(
        "Allow execute_shell for the owner?",
        default=bool(config["system"].get("allow_shell_exec", True)),
    )
    if config["system"]["allow_shell_exec"]:
        mode = _choice(
            "Choose the exec approval mode.",
            [
                ("A", "approval - every command asks for permission"),
                ("B", "list - allowlisted commands run directly; others ask"),
                ("C", "all - every command runs directly"),
            ],
            default={"approval": "A", "list": "B", "all": "C"}.get(
                config["system"].get("exec_approval_mode", "approval"), "A"
            ),
        )
        config["system"]["exec_approval_mode"] = {"A": "approval", "B": "list", "C": "all"}[mode]
        if mode == "B":
            raw = _prompt(
                "Allowlist of commands. Separate with commas. Exact, executable name, or glob.",
                ", ".join(config["system"].get("exec_allowed_commands", [])) or "pwd, ls, python --version",
            )
            config["system"]["exec_allowed_commands"] = [item.strip() for item in raw.split(",") if item.strip()]
    else:
        config["system"]["exec_approval_mode"] = "approval"

    config["system"]["allow_file_edit"] = _confirm(
        "Allow file read/write tools for the owner?",
        default=bool(config["system"].get("allow_file_edit", True)),
    )
    config["system"]["allow_file_send"] = _confirm(
        "Allow the agent to send local files after owner approval?",
        default=bool(config["system"].get("allow_file_send", True)),
    )

    first_run_choice = _choice(
        "Next first-run mode.",
        [
            ("A", "auto"),
            ("B", "interactive"),
            ("C", "panel"),
            ("D", "manual"),
            ("E", "disabled"),
        ],
        default="A",
    )
    config.setdefault("runtime", {})["first_run_mode"] = {
        "A": "auto",
        "B": "interactive",
        "C": "panel",
        "D": "manual",
        "E": "disabled",
    }[first_run_choice]

    if _confirm(
        "Set up browser support now? (Playwright + Chromium, for web/screenshot tools. "
        "Auto-skips on panels/serverless that can't run a browser.)",
        default=False,
    ):
        from cli._shared import install_playwright

        install_playwright(_console.print)


def _save(config: dict[str, Any], config_path: Path) -> bool:
    _console.print("\n[bold cyan]▸ Summary[/bold cyan]")
    masked = mask_config_for_display(config)
    from rich.syntax import Syntax

    _console.print(Syntax(json.dumps(masked, indent=2, ensure_ascii=False), "json", theme="ansi_dark", word_wrap=True))
    _console.print()

    if not _confirm("Save this config?", default=True):
        print("Config not saved.")
        return False

    try:
        AppConfig.model_validate(config)
    except Exception as exc:
        print("Config validation failed:")
        print(exc)
        return False

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.chmod(config_path, 0o600)
    except OSError:
        pass
    _console.print(f"\n[bold green]✅ Config saved:[/bold green] {config_path.resolve()}")
    return True


def run_panel_wizard(config_path: Path | None = None) -> bool:
    """Run the panel-safe wizard and return True if config was saved."""

    if config_path is None:
        config_path = resolve_config_path()
    _banner()
    config = _load_existing_or_template(config_path)
    if config is None:
        return False

    _collect_basics(config)
    _collect_telegram(config)
    _collect_llm(config)
    _collect_mcp(config)
    _collect_system(config)
    return _save(config, config_path)
