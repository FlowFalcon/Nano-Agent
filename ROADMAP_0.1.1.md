# Nano-Agent — v0.1.1 Roadmap

> Current: **v0.1.0** (renamed from micro-agent). This document plans the **v0.1.1**
> feature additions. Items 1–6 were researched in depth (concrete file-level plans);
> items 7–11 are scoped at the design level.
>
> **Guiding principles** (carried from v0.1.0):
> - **Detection over assumption** — gate heavy/host-specific features on `core/runtime.py`
>   (`is_pterodactyl` / `is_serverless` / `is_container`) and `shutil.which()`; never crash,
>   always return a clear hint when a capability is missing.
> - **Safety via the existing HITL loop** — any new dangerous tool is `destructive=True`,
>   so it automatically routes through the Telegram approval keyboard and is auto-excluded
>   from sub-agents/scheduled (unattended) jobs by `is_safe_for_subagent`.
> - **Reuse, don't rebuild** — lean on `_resolve_file_path`, `_cap_output`, `call_llm_simple`,
>   the Scheduler, and the per-request tool-attachment pattern that already exist.

---

## Group A — Execution & Browser

### 1. Sandboxed code execution — `run_code` 🖥️ VPS-only · effort: low
Run Python / JS / Go snippets safely. New tool `run_code(language, code)` in a new
`core/sandbox.py`, reusing the `ExecuteShellTool` subprocess + `wait_for` + `_cap_output`
pattern: fresh `mkdtemp` cwd, **rlimits** (CPU/AS/FSIZE/NPROC via `preexec_fn`), wall-clock
timeout, `os.setsid` + process-group kill, scrubbed env, 50 KB output cap.
- **Sandbox tech:** rlimit subprocess is the always-available floor; if `bwrap` (bubblewrap)
  is on `PATH`, auto-wrap for real namespace isolation (`--unshare-net`, read-only binds, private tmp).
- **Detection:** registered only when `runtime` is **not** Pterodactyl/serverless (thread the
  `runtime` object from `main.py` into `create_default_registry`). Per-language `shutil.which()`.
- **Safety:** `destructive=True` → HITL approval; excluded from sub-agents/scheduler. Known
  ceiling: pure-rlimit can't block sockets — `bwrap`/`nsjail` is the upgrade path.
- New file `core/sandbox.py`; touches `core/tools.py`, `main.py`. No new pip deps.

### 2. Browser automation / headless — `browse_page` + `install_browser` 🌐 effort: medium
Open a URL, scroll, extract text and/or screenshot. New `core/browser_tools.py` with two tools.
- `browse_page(url, action=extract|screenshot|both, scroll, full_page)` — Playwright chromium
  headless; **lazy import** so the dep is optional (missing → install hint, never a crash);
  `asyncio.wait_for` timeout; screenshots saved to `workspace/agent/browser/` and sent via the
  existing approved `send_telegram_file`. Non-destructive (same trust as `fetch_url`).
- `install_browser` — `destructive=True`; runs `python -m playwright install --with-deps chromium`
  but **refuses on Pterodactyl/serverless** (`detect_runtime_environment`).
- **Dependency:** `playwright>=1.40` (pip wheel; the ~200 MB chromium binary is fetched on demand).
- **Safety:** http/https only (SSRF guard like `fetch_url`); `--no-sandbox` for root/containers.

---

## Group B — Files & Vision

### 3. File upload from the user 📎 effort: low
Today only `F.text` is handled, so documents/photos are **silently dropped**. Add a
`@router.message(F.document | F.photo)` handler that downloads via `bot.download()` into
`workspace/agent/uploads/`, then re-enters the normal agent turn with a synthetic prompt
naming the saved relative path. The agent reads it via existing `read_file`/`list_files`/
`execute_shell` (PDF→`pdftotext`, ZIP→`unzip`, both through approval).
- **Reuse:** extract `handle_user_message`'s body into `_run_agent_turn(...)` shared by both
  the text and file handlers (one code path, two entry filters).
- **Safety:** size-cap before download (`max_file_size_mb`, Telegram's ~20 MB getFile ceiling);
  sanitize filename to basename; collision-safe naming. Touches `telegram/handlers.py` only.

### 4. Vision / image understanding 👁️ effort: medium
User sends a Telegram photo → agent passes it to a vision-capable LLM.
- Photo handler downloads to a base64 **data-URL**, threads `images=[...]` into `agent_loop`,
  which attaches OpenAI-style `image_url` content blocks to the user turn.
- **OpenAI path: zero changes** (blocks pass through verbatim). **Anthropic:** add image-block
  translation in `core/wire_anthropic.translate_messages` (`image_url` → `{type:image,source:base64}`),
  plus one round-trip assertion in its self-check.
- History stays text-only (`[photo] caption`) so session JSON / summarization stay simple.
- **Safety:** size-cap before download; image bytes never persisted/logged. Touches
  `telegram/handlers.py`, `core/agent.py`, `core/wire_anthropic.py`.

---

## Group C — Memory

### 5. Automatic periodic memory summarization 🧠 effort: low
The token-threshold summarizer (`auto_summarize_if_needed`) and the Scheduler already exist —
only a **time trigger** is missing. Add `daily_rollup()` in `core/memory.py` that once a day
promotes yesterday's daily note into durable `MEMORY.md` (keep decisions/stable facts/open
loops; drop chatter), guarded by a `<!-- rolled-up -->` idempotency marker. Register one
internal `daily` scheduler job (kind=`memory_rollup`, default 03:30) that calls it directly.
- **Safety:** read-only on conversation, append-only to MEMORY.md, no HITL, no Telegram spam.
- Config knobs `memory_rollup_enabled` / `_hour` / `_minute`. Touches `core/memory.py`, `main.py`, `config/settings.py`.

### 6. Forget / selective memory delete — `forget_memory` 🗑️ effort: low
New `destructive=True` tool: remove **exactly one** matching bullet line from `USER.md` or
`MEMORY.md` (enum-restricted files, substring match, bullets only — never headings).
- **The safety rule:** 0 matches → nothing removed; **>1 matches → refuse** and list candidates
  ("make it more specific"); exactly 1 → remove + report the line. HITL shows the args before write.
- Touches `core/tools.py` only.

---

## Group D — Agent capability

### 7. Git integration — `git_status` / `git_diff` / `git_commit` / `git_branch` / `git_pr` 🔀 effort: medium
Dedicated git tools (new `core/git_tools.py`) instead of raw shell, for structured output +
targeted approval. Read tools (`status`, `diff`, `log`) non-destructive; `commit`/`branch`/
`push`/`pr` `destructive=True`. PRs via `gh` when available.
- **Detection:** `shutil.which("git")` + a repo check (`git rev-parse`); `gh` for PRs (graceful
  message if absent). **Safety:** push/PR gated behind approval; commit message required.
- New file `core/git_tools.py`; touches `core/tools.py`. Optional dep: `gh` CLI (host tool, not pip).

### 8. Multi-step task planner — `plan_task` 🗂️ effort: medium
Let the agent break a complex request into an ordered checklist and work through it.
- Lean approach: a `plan_task(steps[])` tool writes `workspace/agent/PLAN.md` (checkboxes) +
  the agent updates it as it goes; the system prompt instructs "follow and tick the active plan."
  Reuse `spawn_subagent` for isolated sub-steps where helpful. No new heavy machinery.
- **Safety:** non-destructive (writes only its own plan file). Touches `core/tools.py`, prompt in `core/agent.py`.

---

## Group E — Utilities

### 9. Cost & token usage dashboard 📊 effort: medium
Capture the `usage` block from LLM responses in `core/llm.py` (prompt/completion tokens),
accumulate per-day into `workspace/agent/usage.json`, and surface today's totals (and optional
cost via a small per-model price map) in `/status`.
- **Safety:** local counters only. Touches `core/llm.py`, `telegram/handlers.py` (`_status_text`),
  new tiny `core/usage.py`.

### 10. Search workspace files by content — `search_files` 🔎 effort: low
Grep without shell: walk `workspace_dir`, match a query/regex, return `file:line: snippet`.
Skip binaries, respect a size cap and result cap. Pure stdlib → self-checkable.
- **Safety:** read-only, workspace-scoped via `_safe_resolve`. Touches `core/tools.py` (or new `core/search_files.py`).

### 11. Structured output mode (JSON / table) 🧱 effort: low–medium
Let the agent emit a JSON object or a table that Telegram renders cleanly (monospace `<pre>`
table / pretty-printed JSON). A small render helper in `telegram/formatters.py` + an output
convention (or a `present_table(rows)` tool). Optional — plain text stays the default.

---

## Suggested build order

1. **Quick wins first:** #3 file-upload, #6 forget_memory, #10 search_files, #5 memory-rollup (all low effort, high value).
2. **Then VPS/heavy:** #1 run_code, #2 browser, #4 vision.
3. **Then capability/utilities:** #7 git, #9 cost dashboard, #8 planner, #11 structured output.

Each ships behind detection + (where dangerous) the existing approval loop, with a runnable
self-check for any non-trivial logic — same discipline as v0.1.0.
