"""
Rich-based TUI onboarding wizard for the Telegram AI Agent Framework.

Provides a step-by-step interactive configuration wizard that produces
a validated config.json file. Uses Rich for beautiful panels and styling,
and Questionary for interactive prompts (arrows, password masking, etc.).

Security notes:
- Secrets (bot token, API keys) are written to config.json with 0o600
  permissions and are never logged.
- The summary panel masks API keys (first 8 chars + "...").
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import questionary
from questionary import Style
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from config.settings import AppConfig, get_default_config_template, resolve_config_path
from config.version import __project__, __version__
from cli._shared import (
    deep_merge as _deep_merge,
    mask_config_for_display as _mask_config_for_display,
    parse_user_ids as _parse_user_ids,
)

_CONFIG_PATH = resolve_config_path()

console = Console()

WIZARD_STYLE = Style([
    ("qmark", "fg:cyan bold"),
    ("question", "fg:white bold"),
    ("answer", "fg:ansibrightcyan bold"),
    ("pointer", "fg:cyan bold"),
    ("highlighted", "fg:ansibrightcyan bold"),
    ("selected", "fg:green"),
    ("separator", "fg:ansibrightblack"),
    ("instruction", "fg:ansibrightblack"),
])


def _print_header(step: int, total: int, title: str) -> None:
    """Print a styled step header."""
    console.print()
    
    dots = []
    for i in range(1, total + 1):
        if i < step:
            dots.append("●")  # Completed
        elif i == step:
            dots.append("◉")  # Current
        else:
            dots.append("○")  # Pending
            
    progress_str = "  ".join(dots)
    
    header = Text()
    header.append(f"  Step {step}/{total}  ", style="bold white on cyan")
    header.append(f"  {title}\n", style="bold cyan")
    header.append(f"  {progress_str}", style="dim cyan")
    
    console.print(header)
    console.print("  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", style="dim cyan")
    console.print()


def _step_welcome() -> None:
    """Clear the screen and show a simple intro (no ASCII banner)."""
    console.clear()
    console.print(f"[bold bright_cyan]{__project__}[/bold bright_cyan] [dim]v{__version__} · setup[/dim]")
    console.print("[dim cyan]▸ Execute commands  ▸ Edit files  ▸ Browse the web  ▸ Schedule tasks[/dim cyan]")
    console.print("\n[bold yellow]⚡ Let's set up your configuration.[/bold yellow]\n")


def _step_security_notice() -> bool:
    """Display the security warning and ask for acknowledgement."""
    console.print("[bold yellow]🛡️  Security Notice[/bold yellow]")
    console.print("This agent has powerful capabilities:")
    console.print("  [yellow]•[/yellow] [bold white]Execute shell commands[/bold white] on the host system")
    console.print("  [yellow]•[/yellow] [bold white]Read and write files[/bold white] on disk")
    console.print("  [yellow]•[/yellow] [bold white]Make network requests[/bold white] to external services")
    console.print("[bold bright_red]Only grant private bot access to fully trusted owner users.[/bold bright_red]\n")

    return questionary.confirm(
        "I understand the security implications and want to proceed",
        default=True,
        style=WIZARD_STYLE
    ).ask()


def _step_basics(config: dict[str, Any]) -> dict[str, Any]:
    """Collect basic agent setup (like name)."""
    _print_header(1, 5, "🤖 Agent Identity")
    
    config.setdefault("system", {})
    config["system"]["agent_name"] = questionary.text(
        "What should we name your assistant?",
        default=config["system"].get("agent_name", "Nano-Agent"),
        style=WIZARD_STYLE
    ).ask().strip() or "Nano-Agent"
    
    return config


def _step_telegram(config: dict[str, Any], is_quickstart: bool = False) -> dict[str, Any]:
    """Collect Telegram bot configuration."""
    _print_header(2, 5, "📡 Telegram Bot Setup")

    while True:
        bot_token = questionary.password(
            "🔑 Bot Token (from @BotFather):",
            default=config["telegram"].get("bot_token") or "",
            style=WIZARD_STYLE
        ).ask()
        
        if not bot_token:
            console.print("  [red]❌ Bot token cannot be empty.[/red]")
            continue
        parts = bot_token.strip().split(":")
        if len(parts) != 2 or not parts[0].isdigit():
            console.print("  [red]❌ Invalid format. Expected[/red] [bold]<bot_id>:<hash>[/bold]")
            continue
        break
    config["telegram"]["bot_token"] = bot_token.strip()

    while True:
        existing_ids = config["telegram"].get("allowed_user_ids", [])
        default_ids = ",".join(str(i) for i in existing_ids) if existing_ids else ""
        raw_ids = questionary.text(
            "👤 Allowed Telegram User IDs (comma-separated):",
            default=default_ids,
            style=WIZARD_STYLE
        ).ask()
        
        try:
            parsed_ids = _parse_user_ids(raw_ids)
            if not parsed_ids:
                console.print("  [red]❌ At least one allowed user ID is required.[/red]")
                continue
            break
        except ValueError as exc:
            console.print(f"  [red]❌ {exc}[/red]")
    config["telegram"]["allowed_user_ids"] = parsed_ids

    if is_quickstart:
        return config

    mode = questionary.select(
        "⚙️ Bot Mode:",
        choices=["polling", "webhook"],
        default=config["telegram"].get("mode", "polling"),
        style=WIZARD_STYLE
    ).ask()
    config["telegram"]["mode"] = mode

    # Webhook-specific settings
    if mode == "webhook":
        config["telegram"]["webhook_host"] = questionary.text(
            "Bind Host:", default=config["telegram"].get("webhook_host", "0.0.0.0"), style=WIZARD_STYLE
        ).ask()
        
        while True:
            port_str = questionary.text(
                "Bind Port:", default=str(config["telegram"].get("webhook_port", 8080)), style=WIZARD_STYLE
            ).ask()
            try:
                port = int(port_str)
                if not (1 <= port <= 65535):
                    raise ValueError
                break
            except ValueError:
                console.print("    [red]❌ Enter a valid port (1–65535).[/red]")
        config["telegram"]["webhook_port"] = port
        
        config["telegram"]["webhook_path"] = questionary.text(
            "Webhook Path:", default=config["telegram"].get("webhook_path", "/webhook"), style=WIZARD_STYLE
        ).ask()
        
        config["telegram"]["webhook_url"] = questionary.text(
            "Public Webhook URL (e.g. https://yourdomain.com/webhook):",
            default=config["telegram"].get("webhook_url", ""),
            style=WIZARD_STYLE
        ).ask()

    return config


def _collect_llm_provider(label: str, priority: int, defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    """Interactively collect a single LLM provider configuration."""
    if defaults is None:
        defaults = {}

    console.print(f"\n  [bold magenta]{label}[/bold magenta]")

    name = questionary.select(
        "Provider Name:",
        choices=["openrouter", "anthropic", "openai", "google", "deepseek", "groq", "xai", "local"],
        default=defaults.get("name", "openrouter"),
        style=WIZARD_STYLE
    ).ask()

    base_url = questionary.text(
        "Base URL:",
        default=defaults.get("base_url", "https://openrouter.ai/api/v1/chat/completions"),
        style=WIZARD_STYLE
    ).ask()

    while True:
        api_key = questionary.password(
            "🔑 API Key:",
            default=defaults.get("api_key") or "",
            style=WIZARD_STYLE
        ).ask()
        if not api_key:
            console.print("  [red]❌ API key cannot be empty.[/red]")
            continue
        break

    model = questionary.text(
        "Model Name:",
        default=defaults.get("model", "anthropic/claude-3.5-sonnet"),
        style=WIZARD_STYLE
    ).ask()

    return {
        "priority": priority,
        "name": name.strip(),
        "base_url": base_url.strip(),
        "api_key": api_key.strip(),
        "model": model.strip(),
    }


def _step_llm(config: dict[str, Any], is_quickstart: bool = False) -> dict[str, Any]:
    """Collect LLM provider configuration."""
    _print_header(3, 5, "🧠 LLM Provider Setup")

    existing_providers = config["llm"].get("providers", [])
    primary_defaults = existing_providers[0] if existing_providers else None

    primary = _collect_llm_provider("Primary Provider (priority 1)", priority=1, defaults=primary_defaults)
    providers = [primary]

    if not is_quickstart:
        add_fallback = questionary.confirm(
            "Add a fallback LLM provider?", default=False, style=WIZARD_STYLE
        ).ask()

        if add_fallback:
            fallback_defaults = existing_providers[1] if len(existing_providers) > 1 else None
            fallback = _collect_llm_provider("Fallback Provider (priority 2)", priority=2, defaults=fallback_defaults)
            providers.append(fallback)

    config["llm"]["providers"] = providers
    return config


def _step_mcp(config: dict[str, Any]) -> dict[str, Any]:
    """Collect MCP server configuration."""
    _print_header(4, 5, "🔌 MCP Servers (Optional)")

    add_mcp = questionary.confirm(
        "Configure MCP (Model Context Protocol) servers?", default=False, style=WIZARD_STYLE
    ).ask()

    if not add_mcp:
        config["mcp"]["servers"] = config["mcp"].get("servers", [])
        return config

    servers: list[dict[str, Any]] = []

    while True:
        console.print(f"\n  [bold magenta]MCP Server #{len(servers) + 1}[/bold magenta]")

        name = questionary.text("Server Name:", style=WIZARD_STYLE).ask()

        # Only stdio transport is supported (SSE is not implemented).
        server: dict[str, Any] = {"name": name.strip(), "transport": "stdio"}
        server["command"] = questionary.text("Command (e.g. npx, python):", style=WIZARD_STYLE).ask().strip()
        args_raw = questionary.text("Arguments (space-separated, or empty):", default="", style=WIZARD_STYLE).ask()
        server["args"] = args_raw.split() if args_raw.strip() else []
        server["url"] = ""

        server["env"] = {}
        add_env = questionary.confirm("Add environment variables for this server?", default=False, style=WIZARD_STYLE).ask()
        
        if add_env:
            while True:
                env_key = questionary.text("Env var name (or empty to finish):", style=WIZARD_STYLE).ask().strip()
                if not env_key:
                    break
                env_val = questionary.text(f"{env_key} value:", style=WIZARD_STYLE).ask()
                server["env"][env_key] = env_val.strip()

        servers.append(server)

        if not questionary.confirm("Add another MCP server?", default=False, style=WIZARD_STYLE).ask():
            break

    config["mcp"]["servers"] = servers
    return config


def _step_advanced(config: dict[str, Any]) -> dict[str, Any]:
    """Optionally tweak advanced / system settings."""
    _print_header(5, 5, "⚙️  System & Advanced Settings")

    customize = questionary.confirm(
        "Customize advanced settings? (log level, context window, etc.)", default=False, style=WIZARD_STYLE
    ).ask()

    if not customize:
        return config

    console.print("\n  [bold magenta]System[/bold magenta]")
    
    config["system"]["log_level"] = questionary.select(
        "Log Level:", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default=config["system"].get("log_level", "INFO"), style=WIZARD_STYLE
    ).ask()

    shell_exec = questionary.confirm(
        "Allow shell command execution?", default=config["system"].get("allow_shell_exec", True), style=WIZARD_STYLE
    ).ask()
    config["system"]["allow_shell_exec"] = shell_exec

    if shell_exec:
        exec_mode = questionary.select(
            "Shell exec approval mode:", choices=["approval", "list", "all"],
            default=config["system"].get("exec_approval_mode", "approval"), style=WIZARD_STYLE
        ).ask()
        config["system"]["exec_approval_mode"] = exec_mode

        if exec_mode == "list":
            existing = config["system"].get("exec_allowed_commands", [])
            default_value = ", ".join(existing) if existing else "pwd, ls, python --version"
            raw_commands = questionary.text(
                "Allowed shell commands (comma-separated):", default=default_value, style=WIZARD_STYLE
            ).ask()
            config["system"]["exec_allowed_commands"] = [item.strip() for item in raw_commands.split(",") if item.strip()]
        else:
            config["system"].setdefault("exec_allowed_commands", [])

    config["system"]["allow_file_edit"] = questionary.confirm(
        "Allow file read/write?", default=config["system"].get("allow_file_edit", True), style=WIZARD_STYLE
    ).ask()

    config["system"]["allow_file_send"] = questionary.confirm(
        "Allow owner-approved Telegram file sending?", default=config["system"].get("allow_file_send", True), style=WIZARD_STYLE
    ).ask()

    console.print("\n  [bold magenta]Optional Extras[/bold magenta]")

    if questionary.confirm(
        "Set up browser support now? (Playwright + Chromium, for web/screenshot tools. "
        "Auto-skips on hosts that can't run a browser.)",
        default=False,
        style=WIZARD_STYLE,
    ).ask():
        from cli._shared import install_playwright

        install_playwright(console.print)

    return config


def _step_summary_and_save(config: dict[str, Any]) -> bool:
    """Display the final summary and save config.json."""
    console.print()
    console.print("  [bold cyan]📋 Configuration Summary[/bold cyan]")
    console.print("  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", style="dim cyan")

    masked = _mask_config_for_display(config)
    json_str = json.dumps(masked, indent=2)

    syntax = Syntax(json_str, "json", theme="monokai", line_numbers=True)
    console.print(syntax)
    console.print()

    if not questionary.confirm("✅ Save this configuration?", default=True, style=WIZARD_STYLE).ask():
        console.print("  [yellow]Configuration not saved.[/yellow]")
        return False

    try:
        AppConfig.model_validate(config)
    except Exception as exc:
        console.print(f"[bold red]❌ Validation Error[/bold red]\n\n{exc}")
        return False

    config_path = _CONFIG_PATH.resolve()
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    os.chmod(config_path, 0o600)

    console.print("\n[bold green]✅ Configuration saved successfully![/bold green]\n")
    console.print(f"  [white]📄 File:  [bold]{config_path}[/bold][/white]")
    console.print("  [white]🔒 Perms: [dim]0600 (owner read/write only)[/dim][/white]\n")
    console.print("  [white]Start the bot with:[/white]")
    console.print("    [bold bright_cyan]python3 main.py[/bold bright_cyan]\n")

    return True


def _handle_existing_config() -> dict[str, Any] | None:
    """If config.json exists, offer to update or overwrite."""
    if not _CONFIG_PATH.exists():
        return get_default_config_template()

    console.print()
    console.print("[bold yellow]⚠️  An existing config.json was found.[/bold yellow]")

    choice = questionary.select(
        "Choose an option:",
        choices=["Update (keep existing values as defaults)", "Overwrite (start fresh)", "Cancel"],
        style=WIZARD_STYLE
    ).ask()

    if choice == "Cancel":
        console.print("  [dim]Exiting without changes.[/dim]")
        return None

    if choice == "Overwrite (start fresh)":
        return get_default_config_template()

    try:
        raw = _CONFIG_PATH.read_text(encoding="utf-8")
        existing = json.loads(raw)
        template = get_default_config_template()
        return _deep_merge(template, existing)
    except (json.JSONDecodeError, OSError) as exc:
        console.print(f"  [red]❌ Could not parse existing config.json: {exc}[/red]\n  [yellow]Starting with a fresh template instead.[/yellow]")
        return get_default_config_template()


def run_wizard() -> None:
    """Run the interactive configuration wizard."""
    try:
        console.print()
        _step_welcome()

        if not _step_security_notice():
            console.print("\n  [red]❌ You must acknowledge the security notice to continue.[/red]\n")
            sys.exit(1)

        config = _handle_existing_config()
        if config is None:
            sys.exit(0)

        setup_mode = questionary.select(
            "Setup mode:",
            choices=[
                "⚡ QuickStart (bot token + API key Only)",
                "🔧 Advanced (configure all step by step)",
            ],
            style=WIZARD_STYLE,
        ).ask()
        
        is_quickstart = "QuickStart" in setup_mode

        config = _step_basics(config)
        config = _step_telegram(config, is_quickstart)
        config = _step_llm(config, is_quickstart)
        
        if not is_quickstart:
            config = _step_mcp(config)
            config = _step_advanced(config)

        saved = _step_summary_and_save(config)
        if not saved:
            console.print("\n  [dim]Run [bold]python cli.py[/bold] again to restart the wizard.[/dim]\n")

    except KeyboardInterrupt:
        console.print("\n\n  [bold yellow]⚠️  Wizard cancelled by user (Ctrl+C).[/bold yellow]")
        console.print("  [dim]No changes were saved. Run [bold]python cli.py[/bold] to try again.[/dim]\n")
        sys.exit(130)

if __name__ == "__main__":
    run_wizard()
