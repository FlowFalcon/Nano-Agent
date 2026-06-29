# Nano-Agent

> A friendly, beginner-first AI agent that lives in your Telegram chat.

![Python](https://img.shields.io/badge/python-3.12+-blue)
![Status](https://img.shields.io/badge/status-alpha-orange)
![License](https://img.shields.io/badge/license-MIT-green)

🇮🇩 *Versi Bahasa Indonesia: [README_IDN.md](README_IDN.md)*

Nano-Agent is a self-hosted AI agent you talk to on Telegram. It can run shell
commands, read and write files, search and fetch the web, remember things about
you, schedule tasks, **upload and inspect documents / images / ZIPs**, search
its own workspace, forget specific facts, and call external tools over MCP — all
with **human approval** for anything risky. It works with any OpenAI- or
Anthropic-compatible LLM provider.

**What makes it friendly:**

- 🛡️ **Approvals first** — dangerous actions (shell, file writes, deletes) ask before they run, with **reason** + "allow this session" option.
- 🧠 **Persistent memory** — it remembers facts about you and your project in plain Markdown files you can edit.
- 🧩 **File upload** — send documents, images, PDFs, or ZIPs. The agent saves them to its workspace and reads them with the same tools it uses for its own files.
- 🧠 **Self‑aware** — the agent knows its own tools, config, and docs. Ask *"what can you do?"* for a live capability list.
- 💬 **Speaks your language** — the UI is English, but the agent replies in whatever language you write in.

---

## Quickstart

```bash
# 1. Clone and enter
git clone <your-repo-url> nano-agent && cd nano-agent

# 2. Install dependencies (use a virtualenv)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Run the setup wizard (asks for your bot token + LLM key)
python cli.py

# 4. Start the bot
python main.py
```

You'll need a **Telegram bot token** from [@BotFather](https://t.me/BotFather) and an
**LLM API key** (OpenRouter, OpenAI, Anthropic, Groq, etc.). Then message your bot.

> First run with no config? `python main.py` launches the same wizard automatically.

---

## Configuration

Config lives in `config.json` (or `config.yaml`). Secrets can also come from the
environment, so you can keep them out of the file entirely.

Minimal `config.json`:

```json
{
  "telegram": { "bot_token": "", "allowed_user_ids": [123456789] },
  "llm": {
    "providers": [
      {
        "priority": 1,
        "name": "openrouter",
        "base_url": "https://openrouter.ai/api/v1/chat/completions",
        "api_key": "",
        "model": "anthropic/claude-sonnet-4",
        "models": ["anthropic/claude-sonnet-4", "anthropic/claude-3.5-haiku"]
      }
    ]
  }
}
```

Leave secrets blank and supply them via env instead:

| Variable | Fills |
|----------|-------|
| `TELEGRAM_BOT_TOKEN` | `telegram.bot_token` |
| `LLM_API_KEY` | any provider's `api_key` |
| `LLM_API_KEY_<PROVIDER_NAME>` | that provider's `api_key` (e.g. `LLM_API_KEY_OPENROUTER`) |
| `BRAVE_API_KEY`, `TAVILY_API_KEY` | optional web-search fallbacks |

### LLM providers

Any OpenAI- or Anthropic-compatible endpoint works. Set `wire_format` per provider:

| `wire_format` | Endpoint | Examples |
|---------------|----------|----------|
| `openai` (default) | `/chat/completions` | OpenRouter, OpenAI, Groq, DeepSeek, xAI, local (Ollama/llama.cpp), Anthropic's OpenAI-compatible endpoint |
| `anthropic` | `/v1/messages` | Anthropic direct API |

**Fallback is two levels:** for each provider, every model in `models` is tried before
moving to the next provider in the list.

---

## How it works

```
Telegram ──▶ handlers ──▶ agent loop ──▶ LLM provider(s)
                │             │  ▲
                │             ▼  │ tool calls (approval-gated)
            sessions     tool registry ──▶ built-in tools + MCP servers
            (per-topic        │
             history)         ▼
                        workspace memory (Markdown brain + daily notes)
```

- **Agent loop** (`core/agent.py`) streams the model's reply, runs any tool calls it
  requests, and feeds results back until the task is done — capped by `max_iterations`.
  Long conversations are **auto-summarized** when they cross the token threshold.
- **Tool registry** (`core/tools.py`) exposes the built-in tools below; MCP servers and
  the sub-agent tool register into the same registry, so everything is one flat toolset.
- **Approvals** (`core/shell_policy.py`, subagent policy) gate anything risky before it
  runs, with a reason and per-session "allow" memory.
- **Workspace memory** (`core/memory.py`) is the Markdown brain — loaded into context
  each turn, updated as the agent learns.
- **Runtime detection** (`core/runtime.py`) recognizes VPS, container, Pterodactyl panel,
  and serverless hosts, and adapts first-run setup and browser install to each.
- **Per-topic sessions** (`telegram/sessions.py`) keep a separate history per Telegram
  topic/thread, so parallel conversations don't bleed into each other.

---

## Tools

The agent has these built-in tools (MCP servers add more):

| Tool | Purpose | Needs approval |
|------|---------|:---:|
| `execute_shell` | Run a shell command | ✅ (per shell mode, with reason) |
| `read_file` / `write_file` / `replace_in_file` | Read / write / edit files | write/edit: ✅ |
| `list_files` | List a directory (optional glob) | — |
| `search_files` | Search file contents (grep without shell) | — |
| `make_directory` / `delete_file` | Create / delete | delete: ✅ |
| `web_search` | Search the web (DuckDuckGo → Brave → Tavily) | — |
| `fetch_url` | Fetch a URL's text | — |
| `get_current_time` | Current date/time | — |
| `forget_memory` | Remove a single fact line from USER.md or MEMORY.md | ✅ (with 1-line guard) |
| `update_user_fact` / `update_project_memory` | Persist memory to `USER.md` / `MEMORY.md` | — |
| `ask_user` | Ask you a clarifying question (with buttons) | — |
| `spawn_subagent` | Delegate a subtask to a restricted sub-agent | — |
| `schedule_task` / `list_schedules` / `cancel_schedule` | Schedule reminders/jobs | — |

### Self‑aware agent

Nano-Agent **knows its own capabilities**. It reads the tool list and its own config,
docs, and roadmap files to understand what it can do. Just ask *"what tools do you have?"*
or *"check my config"* and it responds with live information about its setup.

### Browser support (optional)

The setup wizard can install **Playwright + Chromium** for web/screenshot tools. The
install is **environment-aware**:

- **VPS / container / local** — installs the Playwright wheel and the Chromium binary
  (plus the OS libraries it needs when running as root), then verifies it launches.
- **Pterodactyl panel / serverless** — **auto-skips** with a clear message, since those
  hosts usually can't run a browser. You'll get a copy-paste command to try later if your
  host does support it.

You can install it later from the wizard at any time; nothing else depends on it.

---

## Skills & Memory

**Skills** are small Markdown playbooks the agent loads automatically when your message
matches their trigger words. Drop them in `workspace/agent/skills/*.md`:

```markdown
---
triggers: deploy, ship it, release
---
# Deploy playbook
Run tests → bump version → tag → push.
```

**Memory** is a "brain" of editable Markdown files in `workspace/agent/` that the
agent loads into its context on every turn:

| File | Holds |
|------|-------|
| `IDENTITY.md` / `SOUL.md` | Who the agent is and how it behaves |
| `USER.md` | Durable facts about you |
| `RELATIONSHIP.md` | How you two work together — tone, preferences, corrections |
| `MEMORY.md` | Project decisions and long-lived notes |
| `AGENTS.md` | Operating rules for the agent |
| `memory/YYYY-MM-DD.md` | Daily episodic notes |

You can edit any of these by hand; the agent reads and updates them as it learns.

### Reads its workspace on start

On boot the agent scans its workspace home and embeds a **live snapshot** (files,
sizes, folders) into its context, so it knows what it's already saved and what it
has to work with — it isn't limited to a static prompt.

### Develops as you use it

At session boundaries (`/new`, or when a long chat is auto-summarized) the agent runs
**one cheap reflection pass** over the conversation and writes back what it learned:
new facts to `USER.md`, working-style notes to `RELATIONSHIP.md`, and decisions to
`MEMORY.md`. Over time it gets more tailored to you without you managing anything.

---

## Scheduling

Just ask in natural language:

> "Remind me to check email every day at 5pm."

The agent registers a job that runs unattended (with a safe, non-destructive toolset)
and messages you the result. Manage jobs via `list_schedules` / `cancel_schedule`.

---

## MCP servers

Connect external tool servers (Model Context Protocol, stdio transport). Add them under
`mcp.servers` in your config; their tools are merged into the agent automatically.

```json
{ "mcp": { "servers": [
  { "name": "fs", "transport": "stdio", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"] }
] } }
```

---

## Slash commands

The command menu is intentionally small — essentials plus owner-only admin controls.

| Command | What it does |
|---------|--------------|
| `/help` (and `/start`) | Help and command list |
| `/new` | Start a fresh session — also triggers a memory reflection (see below) |
| `/stop` | Stop the running task |
| `/restart` | Restart the bot |
| `/status` | Inspect agent state (model, mode, tools, jobs) |
| `/topic <name>` | Create/switch a Telegram topic (tip; not shown in the menu) |
| `/model [m]` | **Admin** — view/change the primary model |
| `/shell [ask\|list\|all]` | **Admin** — view/change shell safety mode |
| `/allow [cmd]` | **Admin** — view/add to the shell allowlist |
| `/block [cmd]` | **Admin** — view/add to the shell blocklist |
| `/mcp` | **Admin** — list MCP servers |
| `/skills` | **Admin** — list active skills |

Admin commands are owner-only and persist their changes to your config immediately.

---

## Safety & approvals

Shell execution has three modes (`/shell`):

- **ask** (default) — every command asks for approval (with an explanation *why*).
- **list** — commands on the allowlist run directly; everything else asks.
- **all** — everything runs without asking (use with care).

### Approval with reason

When a tool needs your approval, the agent tells you **why** — e.g. *"'rm -rf /' is on the
blocklist of dangerous commands"* or *"'write_file' modifies files — always requires
confirmation."* Two buttons are available:

- **✅ Run once** — approve just this one call.
- **🔄 Allow this session** — approve the same command (or tool+args) for the rest of this
  conversation without asking again. Resets on your next message.
- **❌ Deny** — reject this call.

### Blocklist

A **blocklist** (`/block`) always forces approval for matching commands — even in
`all` mode (e.g. block `rm`, `shutdown`). The allowlist guards against shell-operator
bypass: `ls && rm -rf /` will not sneak through an `ls` entry.

---

## Running in production

- **Docker** (recommended): `docker compose up -d`. The compose file mounts
  `./workspace` (persistent memory) and sets `restart: unless-stopped`.
- **Bare-metal**: `python cli/manage.py start | stop | status | doctor`.
- **Pterodactyl / panels**: the first-run wizard auto-detects panel consoles and uses
  a plain A/B/C prompt flow.

`python cli/manage.py doctor` validates your config and checks that secrets are present.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Wizard/CLI won't start | `pip install -r requirements.txt` inside your venv |
| "Web search unavailable" | DuckDuckGo rate-limits VPS IPs — set `BRAVE_API_KEY` or `TAVILY_API_KEY` |
| Config errors on boot | `python cli/manage.py doctor` |
| Bot silent | Check your token and that your user ID is in `allowed_user_ids` |

---

## License

MIT. See `LICENSE`.
