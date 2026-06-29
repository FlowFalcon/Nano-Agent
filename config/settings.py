"""
Configuration schema and loader for the Telegram AI Agent Framework.

Defines the typed config (Telegram, LLM providers, MCP, system settings) and
loads it from config.json, filling blank secrets from environment variables.
Only users listed in ``telegram.allowed_user_ids`` may use the bot; shell
approval has three modes (approval, list, all); owner file-sending always
requires Telegram approval.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from config.env_overrides import apply_env_overrides

logger = logging.getLogger(__name__)


class SystemConfig(BaseModel):
    """System-level configuration."""

    agent_name: str = Field(default="Nano-Agent", description="Name of the agent")
    log_level: str = Field(default="INFO", description="Python logging level")
    allow_shell_exec: bool = Field(default=True, description="Allow shell command execution")
    exec_approval_mode: Literal["approval", "list", "all"] = Field(
        default="approval",
        description=(
            "Shell execution approval policy. "
            "'approval' asks before every execute_shell command; "
            "'list' runs commands in exec_allowed_commands without approval and asks for the rest; "
            "'all' runs all execute_shell commands without asking. "
            "allow_shell_exec=false still disables the execute_shell tool entirely."
        ),
    )
    exec_allowed_commands: list[str] = Field(
        default_factory=list,
        description=(
            "Allowlist used when exec_approval_mode='list'. Entries can be exact full commands, "
            "base executable names such as 'ls', or shell-style glob patterns such as 'python *'."
        ),
    )
    exec_blocked_commands: list[str] = Field(
        default_factory=list,
        description=(
            "Blocklist: commands that ALWAYS require approval regardless of exec_approval_mode. "
            "Aggressive match — a bare name like 'rm' blocks any command containing it as a token "
            "(so 'ls && rm -rf /' is caught). Use for dangerous executables."
        ),
    )
    allow_file_edit: bool = Field(default=True, description="Allow file read/write operations")
    workspace_dir: str = Field(default="workspace/agent", description="Workspace directory for agent files and memory")
    max_file_size_mb: int = Field(default=50, ge=1, le=500, description="Maximum file size in MB for read/write tools")
    allow_file_send: bool = Field(
        default=True,
        description=(
            "Allow the owner-only agent to send local files to Telegram. "
            "Every file send is treated as a HITL approval action."
        ),
    )
    max_send_file_size_mb: int = Field(
        default=50,
        ge=1,
        le=50,
        description="Maximum local file size in MB that send_telegram_file may upload via Bot API sendDocument",
    )

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid_levels = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
        upper = v.upper()
        if upper not in valid_levels:
            raise ValueError(f"log_level must be one of {valid_levels}, got '{v}'")
        return upper


class RuntimeConfig(BaseModel):
    """Runtime and first-run setup behavior."""

    auto_detect: bool = Field(default=True, description="Detect VPS/container/panel/serverless runtime on startup")
    profile: Literal["auto", "local_interactive", "headless_server", "docker_container", "pterodactyl", "serverless", "unknown"] = Field(
        default="auto",
        description="Runtime profile override. Keep 'auto' unless you need to force a setup mode.",
    )
    first_run_mode: Literal["auto", "interactive", "panel", "manual", "disabled"] = Field(
        default="auto",
        description=(
            "What to do when config.json is missing. "
            "auto chooses Rich interactive setup on normal VPS/TTY, panel setup on Pterodactyl, "
            "and manual instructions on serverless/non-interactive hosts."
        ),
    )
    continue_after_wizard: bool = Field(
        default=True,
        description="After a successful first-run wizard, continue booting main.py when the runtime supports it",
    )


class TelegramConfig(BaseModel):
    """Telegram bot configuration."""

    model_config = ConfigDict(extra="ignore")

    bot_token: str = Field(..., description="Telegram Bot API token")
    allowed_user_ids: list[int] = Field(
        ...,
        min_length=1,
        description="Telegram user IDs allowed to use the private owner bot",
    )
    stream_delay_seconds: float = Field(default=1.5, ge=0.5, le=5.0, description="Debounce delay for draft updates")
    enable_private_draft_streaming: bool = Field(
        default=True,
        description="Use sendMessageDraft for private chats with allowed users",
    )
    mode: Literal["polling", "webhook"] = Field(default="polling", description="Bot mode: polling or webhook")
    webhook_host: str = Field(default="0.0.0.0", description="Webhook server bind host")
    webhook_port: int = Field(default=8080, ge=1, le=65535, description="Webhook server bind port")
    webhook_path: str = Field(default="/webhook", description="Webhook URL path")
    webhook_url: str = Field(default="", description="Full public webhook URL, e.g. https://yourdomain.com")

    unauthorized_message: str = Field(
        default="This bot is only available to allowed owner users.",
        description="Message sent to normal Telegram users outside allowed_user_ids",
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_admin_ids(cls, data: Any) -> Any:
        """Accept old config.json files once, but normalize to allowed_user_ids."""
        if isinstance(data, dict):
            data = dict(data)
            if "allowed_user_ids" not in data and "admin_ids" in data:
                data["allowed_user_ids"] = data.get("admin_ids") or []
            data.pop("admin_ids", None)
            # Ignore unsupported guest-bot keys so older config files still load.
            data.pop("guest_bot_allow_public", None)
            data.pop("guest_bot_allowed_user_ids", None)
            data.pop("guest_bot_use_workspace_memory", None)
            data.pop("guest_bot_allow_tools", None)
        return data

    @field_validator("bot_token")
    @classmethod
    def validate_bot_token(cls, v: str) -> str:
        if not v or v == "YOUR_BOT_TOKEN":
            raise ValueError("bot_token must be set to a valid Telegram Bot token. Run 'python cli.py' to configure.")
        parts = v.split(":")
        if len(parts) != 2 or not parts[0].isdigit():
            raise ValueError("bot_token format invalid. Expected format: '<bot_id>:<alphanumeric_hash>'")
        return v


class LLMProvider(BaseModel):
    """Single LLM provider configuration."""

    priority: int = Field(..., ge=1, description="Provider priority (1 = highest)")
    name: str = Field(..., min_length=1, description="Human-readable provider name")
    base_url: str = Field(..., description="Base URL for the chat completions endpoint")
    api_key: str = Field(..., min_length=1, description="API key for this provider")
    model: str = Field(..., min_length=1, description="Model identifier")
    models: list[str] = Field(
        default_factory=list,
        description="Optional extra fallback models for THIS provider, tried after `model` before switching providers.",
    )
    wire_format: Literal["openai", "anthropic"] = Field(
        default="openai",
        description="API wire format: 'openai' (/chat/completions) or 'anthropic' (/v1/messages, native).",
    )
    extra_body: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional provider-specific JSON fields merged into chat/completions payload. Use only when documented by the provider.",
    )


class LLMConfig(BaseModel):
    """LLM configuration with multi-provider support."""

    context_window_tokens: int = Field(default=16000, ge=1000, description="Maximum context window in tokens")
    summarization_threshold_tokens: int = Field(default=4000, ge=500, description="Token threshold for auto-summarization")
    max_iterations: int = Field(default=20, ge=1, le=100, description="Max agent loop iterations")
    providers: list[LLMProvider] = Field(..., min_length=1, description="Ordered list of LLM providers")

    @field_validator("providers")
    @classmethod
    def sort_by_priority(cls, v: list[LLMProvider]) -> list[LLMProvider]:
        return sorted(v, key=lambda p: p.priority)


class MCPServerConfig(BaseModel):
    """Configuration for a single MCP server."""

    name: str = Field(..., min_length=1, description="Server name identifier")
    transport: Literal["stdio", "sse"] = Field(default="stdio", description="Transport protocol")
    command: str = Field(default="", description="Command to launch stdio server")
    args: list[str] = Field(default_factory=list, description="Arguments for stdio server command")
    env: dict[str, str] = Field(default_factory=dict, description="Environment variables for the server process")
    url: str = Field(default="", description="SSE server URL for transport='sse'")


class MCPConfig(BaseModel):
    """MCP servers configuration."""

    servers: list[MCPServerConfig] = Field(default_factory=list, description="List of MCP servers to connect to")


class AppConfig(BaseModel):
    """Root application configuration."""

    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    system: SystemConfig = Field(default_factory=SystemConfig)
    telegram: TelegramConfig
    llm: LLMConfig
    mcp: MCPConfig = Field(default_factory=MCPConfig)


_CONFIG_PATH_ENV = "AGENT_CONFIG_PATH"
_DEFAULT_CONFIG_PATH = Path("config.json")


def resolve_config_path(path: Path | None = None) -> Path:
    """Resolve the active config path, honoring AGENT_CONFIG_PATH."""
    if path is not None:
        return Path(path).resolve()
    env_path = os.environ.get(_CONFIG_PATH_ENV)
    return (Path(env_path) if env_path else _DEFAULT_CONFIG_PATH).resolve()


def _read_config_dict(path: Path) -> dict[str, Any]:
    """Parse a config file by extension: .yaml/.yml via YAML, otherwise JSON."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml  # lazy: only needed when a YAML config is actually used
        return yaml.safe_load(text) or {}
    return json.loads(text)


def _resolve_existing_config(path: Path | None) -> Path:
    """Pick which config file to load. Honors an explicit path / AGENT_CONFIG_PATH;
    otherwise prefers config.yaml, then config.yml, then config.json."""
    if path is not None:
        return Path(path).resolve()
    env_path = os.environ.get(_CONFIG_PATH_ENV)
    if env_path:
        return Path(env_path).resolve()
    for candidate in ("config.yaml", "config.yml", "config.json"):
        resolved = Path(candidate).resolve()
        if resolved.exists():
            return resolved
    return _DEFAULT_CONFIG_PATH.resolve()


def load_config(path: Path | None = None) -> AppConfig:
    """Load and validate configuration from JSON or YAML."""
    path = _resolve_existing_config(path)

    if not path.exists():
        logger.error("Configuration file not found at: %s", path)
        print(
            f"\n[ERROR] Configuration file not found at: {path}\n"
            f"Please run the setup wizard first:\n"
            f"  python cli.py\n",
            file=sys.stderr,
        )
        raise FileNotFoundError(f"Configuration file not found at '{path}'. Run 'python cli.py' to create one.")

    raw_data = apply_env_overrides(_read_config_dict(path))
    config = AppConfig.model_validate(raw_data)

    if isinstance(raw_data.get("telegram"), dict) and "admin_ids" in raw_data["telegram"]:
        logger.warning("config.telegram.admin_ids is deprecated; use allowed_user_ids instead.")

    logger.info("Configuration loaded successfully from %s", path)
    return config


def get_default_config_template() -> dict[str, Any]:
    """Return a default config template for the CLI wizard to populate."""
    return {
        "runtime": {
            "auto_detect": True,
            "profile": "auto",
            "first_run_mode": "auto",
            "continue_after_wizard": True,
        },
        "system": {
            "log_level": "INFO",
            "allow_shell_exec": True,
            "exec_approval_mode": "approval",
            "exec_allowed_commands": [
                "pwd",
                "ls",
                "python --version",
            ],
            "exec_blocked_commands": [],
            "allow_file_edit": True,
            "workspace_dir": "workspace/agent",
            "max_file_size_mb": 50,
            "allow_file_send": True,
            "max_send_file_size_mb": 50,
        },
        "telegram": {
            "bot_token": "",
            "allowed_user_ids": [],
            "stream_delay_seconds": 1.5,
            "enable_private_draft_streaming": True,
            "mode": "polling",
            "webhook_host": "0.0.0.0",
            "webhook_port": 8080,
            "webhook_path": "/webhook",
            "webhook_url": "",
            "unauthorized_message": "This bot is only available to allowed owner users.",
        },
        "llm": {
            "context_window_tokens": 16000,
            "summarization_threshold_tokens": 4000,
            "max_iterations": 20,
            "providers": [],
        },
        "mcp": {
            "servers": [],
        },
    }


def save_config_update(updater: Callable[[dict[str, Any]], None]) -> AppConfig:
    """Apply *updater* to the on-disk config dict, validate, and write it back
    atomically (preserving JSON/YAML format). Returns the new validated config.

    Secrets supplied via environment variables are NOT written to the file — only
    the file's own values are persisted.
    """
    path = _resolve_existing_config(None)
    raw = _read_config_dict(path) if path.exists() else get_default_config_template()
    updater(raw)
    config = AppConfig.model_validate(apply_env_overrides(raw))  # validate (env fills secrets)

    tmp = path.with_name(path.name + ".tmp")
    if path.suffix.lower() in (".yaml", ".yml"):
        import yaml
        tmp.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    else:
        tmp.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    logger.info("Config updated and saved to %s", path)
    return config
