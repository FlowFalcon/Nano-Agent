#!/usr/bin/env python3
"""
Main entry point for the Telegram AI Agent Framework.
Initializes the bot, dispatcher, loads configuration, sets up core systems
like MCP, and starts polling or webhook mode.
"""

import asyncio
import logging
import os
import sys

from config.settings import load_config, resolve_config_path
from core.runtime import detect_runtime_environment, format_runtime_summary, print_manual_config_help
from config.version import __project__, __version__


def _run_rich_wizard_or_exit(config_path) -> None:
    """Run the synchronous Rich wizard; exit with manual help if no config resulted."""
    from cli.dashboard import run_wizard

    run_wizard()  # synchronous (questionary); do NOT await
    if not config_path.exists():
        print_manual_config_help(config_path)
        raise SystemExit(1)


async def _ensure_first_run_config() -> None:
    """Create config.json on first run when the host supports a wizard."""

    config_path = resolve_config_path()
    if config_path.exists():
        return

    runtime = detect_runtime_environment()
    first_run_mode = os.environ.get("AGENT_FIRST_RUN_MODE", "auto").strip().lower()
    if first_run_mode not in {"auto", "interactive", "panel", "manual", "disabled"}:
        first_run_mode = "auto"

    print()
    print("No config file found. Starting first-run setup...")
    print(format_runtime_summary(runtime))
    print(f"reason={runtime.reason}")
    print(f"first_run_mode={first_run_mode}")
    print()

    if first_run_mode in {"manual", "disabled"}:
        print_manual_config_help(config_path)
        raise SystemExit(1)

    if first_run_mode == "panel":
        from cli.panel_wizard import run_panel_wizard

        saved = run_panel_wizard(config_path)
        raise SystemExit(0 if saved else 1)

    if first_run_mode == "interactive":
        _run_rich_wizard_or_exit(config_path)
        return

    # auto mode: serverless and unknown non-interactive environments should not
    # block forever on input. They should be configured manually via config.json
    # or environment-mounted files/secrets.
    if runtime.is_serverless or (not runtime.is_interactive and not runtime.is_pterodactyl):
        print_manual_config_help(config_path)
        raise SystemExit(1)

    if runtime.is_pterodactyl:
        from cli.panel_wizard import run_panel_wizard

        saved = run_panel_wizard(config_path)
        if not saved:
            raise SystemExit(1)

        print()
        print("Panel setup finished. Please restart the server from your panel to start the bot.")
        print("This avoids running the bot inside a half-initialized panel startup session.")
        print()
        raise SystemExit(0)

    # Normal VPS/local TTY: use the Rich wizard, then continue booting main.py.
    _run_rich_wizard_or_exit(config_path)


async def _register_bot_commands(bot) -> None:
    """Register the / command menu and point the Menu button at it.

    Telegram caches the command list per client, so if the menu still looks
    empty right after this, fully close and reopen the chat (or the app).
    """
    from aiogram.types import BotCommand, BotCommandScopeAllPrivateChats, MenuButtonCommands

    commands = [
        BotCommand(command="help", description="Guide and command list"),
        BotCommand(command="new", description="Start a fresh session"),
        BotCommand(command="stop", description="Stop the active task"),
        BotCommand(command="restart", description="Restart the bot"),
        BotCommand(command="status", description="Agent status"),
        BotCommand(command="model", description="View/change model"),
        BotCommand(command="shell", description="View/change shell mode"),
        BotCommand(command="allow", description="View/add allowlist"),
        BotCommand(command="block", description="View/add blocklist"),
        BotCommand(command="mcp", description="List MCP servers"),
        BotCommand(command="skills", description="List active skills"),
    ]
    # Default scope covers groups/forums; the private-chat scope guarantees the
    # owner's DM menu is populated even if a stale default scope lingers.
    await bot.set_my_commands(commands)
    await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
    # Make sure the blue "Menu" button shows the command list (not a Web App).
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())


def _print_ready_panel(console, config, *, runtime, tool_count, skill_count, job_count) -> None:
    """Full 'ready to serve' summary shown once, when the bot starts listening."""
    from rich.panel import Panel
    from rich.table import Table

    llm = config.llm
    primary = llm.providers[0]  # providers are sorted by priority
    fallbacks = [p.model for p in llm.providers[1:]]
    fallback_label = (
        ", ".join(fallbacks[:2]) + (" …" if len(fallbacks) > 2 else "")
        if fallbacks else "none"
    )
    host_label = runtime.profile + (" · root" if runtime.is_root else "")
    shell_label = config.system.exec_approval_mode if config.system.allow_shell_exec else "disabled"

    sections = [
        ("Agent", config.system.agent_name),
        ("Host", host_label),
        ("Transport", config.telegram.mode),
        (None, None),
        ("Model", f"{primary.model}  [dim]({primary.name})[/dim]"),
        ("Fallback", fallback_label),
        ("Context", f"{llm.context_window_tokens:,} tok  [dim]· summarize @ {llm.summarization_threshold_tokens:,}[/dim]"),
        (None, None),
        ("Shell", shell_label),
        ("Tools", str(tool_count)),
        ("Skills", str(skill_count)),
        ("MCP", f"{len(config.mcp.servers)} server(s)" if config.mcp.servers else "none"),
        ("Jobs", f"{job_count} scheduled" if job_count else "none"),
        ("Owners", f"{len(config.telegram.allowed_user_ids)} user(s)"),
        ("Workspace", config.system.workspace_dir),
    ]
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="right", style="dim")
    grid.add_column(style="bright_white")
    for key, value in sections:
        grid.add_row("", "") if key is None else grid.add_row(key, str(value))

    console.print(Panel(
        grid,
        title=f"🤖 [bold bright_cyan]{__project__}[/bold bright_cyan] [dim]v{__version__}[/dim]",
        subtitle="[green]● listening[/green] [dim]— Ctrl+C to stop[/dim]",
        border_style="bright_cyan",
        expand=True,
        padding=(1, 2),
    ))


async def main() -> None:
    await _ensure_first_run_config()

    try:
        config = load_config()
    except FileNotFoundError:
        # load_config already printed an error message
        sys.exit(1)
        
    from rich.logging import RichHandler
    from rich.console import Console

    console = Console()
    console.clear()

    logging.basicConfig(
        level=config.system.log_level,
        format="%(message)s",
        handlers=[RichHandler(
            console=console,
            rich_tracebacks=True,
            show_time=False,
            show_path=False,
            markup=True,
        )],
    )
    logger = logging.getLogger(__name__)

    console.print(f"[dim]Starting {__project__} v{__version__}…[/dim]\n")

    # Import runtime dependencies only after first-run setup.
    # This lets a fresh server create config.json even before all bot/runtime
    # dependencies are installed.
    from aiogram import Bot, Dispatcher
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
    from aiohttp import web

    from core.memory import WorkspaceMemory
    from core.mcp import MCPManager
    from core.tools import create_default_registry
    from telegram.handlers import router
    from telegram.middleware import AllowedUsersMiddleware

    memory = WorkspaceMemory(config.system.workspace_dir)
    await memory.initialize(agent_name=config.system.agent_name)
    logger.info("Workspace memory initialized at %s", config.system.workspace_dir)

    runtime = detect_runtime_environment()
    logger.info("Read workspace home:\n%s", memory.scan_workspace())

    tools = create_default_registry(
        workspace_dir=config.system.workspace_dir,
        allow_shell=config.system.allow_shell_exec,
        allow_file_edit=config.system.allow_file_edit,
        max_file_size_mb=config.system.max_file_size_mb,
    )

    # Subagent tool: delegates focused subtasks to a reduced, non-destructive
    # toolset (and cannot spawn further subagents). Holds a ref to `tools` and
    # prunes at call time, so it also sees any MCP tools registered below.
    from core.subagent import SubagentTool
    tools.register(SubagentTool(memory=memory, config=config, base_registry=tools))

    mcp_manager = MCPManager()
    if config.mcp.servers:
        logger.info("Initializing %d MCP servers...", len(config.mcp.servers))
        await mcp_manager.initialize(config.mcp.servers, tools)

    bot = Bot(token=config.telegram.bot_token)
    try:
        await _register_bot_commands(bot)
        logger.info("Command menu registered (reopen the chat if it still looks empty — Telegram caches it).")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not register command menu: %s — /help still lists every command.", exc)

    dp = Dispatcher()

    dp["config"] = config
    dp["tools"] = tools
    dp["memory"] = memory
    dp["runtime_info"] = format_runtime_summary(runtime)

    # Scheduler: a background loop fires scheduled jobs (reminders, daily briefings,
    # monitoring). Jobs run the agent with a non-destructive toolset (unattended → no
    # approver) and the result is sent to the job's owner chat.
    from pathlib import Path

    from core.scheduler import Scheduler
    from core.subagent import prune_registry_for_subagent

    scheduler = Scheduler(Path(config.system.workspace_dir) / "schedules.json")
    _scheduled_tools = prune_registry_for_subagent(tools)

    async def _fire_scheduled_job(job: dict) -> None:
        from core.agent import run_agent_once

        result = await run_agent_once(
            job.get("prompt", ""),
            tools=_scheduled_tools,
            memory=memory,
            config=config,
            include_private_memory=True,
            allow_tools=True,
        )
        chat_id = job.get("chat_id")
        if chat_id and result:
            try:
                await bot.send_message(chat_id, result[:4000])
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to send scheduled job result: %s", exc)

    asyncio.create_task(scheduler.run_forever(_fire_scheduled_job))
    dp["scheduler"] = scheduler
    logger.info("Scheduler initialized with %d job(s).", len(scheduler.jobs))

    # Add middleware to private/group message and callback paths (owner-only).
    allowed_middleware = AllowedUsersMiddleware(
        config.telegram.allowed_user_ids,
        config.telegram.unauthorized_message,
    )
    dp.message.middleware(allowed_middleware)
    dp.callback_query.middleware(allowed_middleware)
    
    dp.include_router(router)

    from core.skills import load_skills_dir

    panel_data = dict(
        runtime=runtime,
        tool_count=len(tools.list_names()),
        skill_count=len(load_skills_dir(Path(config.system.workspace_dir) / "skills")),
        job_count=len(scheduler.jobs),
    )

    try:
        if config.telegram.mode == "polling":
            await bot.delete_webhook(drop_pending_updates=True)
            _print_ready_panel(console, config, **panel_data)
            await dp.start_polling(
                bot,
                allowed_updates=dp.resolve_used_update_types(),
            )
        elif config.telegram.mode == "webhook":
            logger.info("Starting bot in WEBHOOK mode on %s:%d...", config.telegram.webhook_host, config.telegram.webhook_port)
            await bot.set_webhook(
                url=f"{config.telegram.webhook_url}{config.telegram.webhook_path}",
                drop_pending_updates=True,
                allowed_updates=dp.resolve_used_update_types(),
            )
            app = web.Application()
            webhook_requests_handler = SimpleRequestHandler(
                dispatcher=dp,
                bot=bot,
            )
            webhook_requests_handler.register(app, path=config.telegram.webhook_path)
            setup_application(app, dp, bot=bot)
            
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, config.telegram.webhook_host, config.telegram.webhook_port)
            await site.start()

            _print_ready_panel(console, config, **panel_data)
            while True:
                await asyncio.sleep(3600)
        else:
            logger.error("Unknown telegram mode: %s", config.telegram.mode)
            sys.exit(1)
            
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by user.")
    finally:
        logger.info("Shutting down...")
        await mcp_manager.shutdown()
        if config.telegram.mode == "webhook":
            await bot.delete_webhook()
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
