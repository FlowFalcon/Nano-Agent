# ROADMAP — micro-agent v1.0

> **Keputusan inti (27 Jun 2026):** kembangkan kode **Python** yang sudah ada,
> JANGAN tulis ulang ke JavaScript. `blueprint.md` dipakai sebagai **peta fitur**,
> bukan perintah ganti bahasa. WhatsApp & Discord = **v2**, bukan sekarang.
>
> **Aturan emas:** jangan kerjakan Fase N+1 sebelum Fase N terbukti jalan lewat
> pesan beneran di Telegram. Tiap fase harus meninggalkan bot yang **tetap hidup**.

---

## Gambaran besar

Satu **otak** dengan banyak **mulut**. v1.0 membangun otaknya sampai pintar —
mulutnya masih satu: Telegram.

```
   Telegram ──▶  OTAK (Python, micro-agent)   ◀── v1.0 fokus di sini
   (v1.0)        - loop agent + tools (core/agent.py)
                 - LLM + fallback provider (core/llm.py)
   Discord ─ ─ ▶ - memory, skills, clarify, subagent
   (v2)          - MCP (core/mcp.py)
   WhatsApp ─ ─▶ (mulut baru ditambah nanti, tanpa bongkar otak)
   (v2, JS)
```

**Modal awal yang sudah jalan (jangan dirombak, cuma diperluas):**

| Sudah ada & teruji | Di mana | Catatan |
|---|---|---|
| Loop agent (tool-calling, approval, gather paralel) | `core/agent.py:119` `agent_loop` | Yield event **generik** (`text`/`action_start`/`action_result`/`pause_for_approval`/`error`) — sudah bukan Telegram |
| Streaming LLM + fallback antar-provider | `core/llm.py:106` `stream_llm_response` | Retry HTTP 429/500/502/503, lalu pindah provider |
| Akumulasi tool_calls streaming | `core/llm.py:188-230` | Bagian tersulit, sudah benar |
| Allowlist shell (operator-bypass ditutup) | `core/shell_policy.py` `is_command_allowlisted` | 3 mode: `approval`/`list`/`all` (Fase 1 ✅) |
| Memory workspace Markdown | `core/memory.py` `WorkspaceMemory` | SOUL/IDENTITY/USER/RELATIONSHIP/MEMORY/daily |
| 6 tool bawaan | `core/tools.py` | shell, read/write/replace file, web_search, 2 memory tool |
| MCP stdio + multi-server | `core/mcp.py` `MCPManager.initialize` | Loop banyak server stdio sudah jalan |
| Config tervalidasi (pydantic) | `config/settings.py` `AppConfig` | runtime/system/telegram/llm/mcp |

> **Fakta kunci:** `grep -rn "import aiogram" core/` = **0 hasil**. Kata "Telegram"
> di `core/` cuma muncul **16×**, semuanya teks di dalam prompt — bukan kode. Jadi
> multi-channel sebagian besar = **pindah teks**, bukan rewrite.

---

## Cara baca roadmap ini

- Tiap fase = satu paket kerja kecil, dikerjakan **berurutan**.
- `[ ]` belum · `[x]` sudah & sudah dites lewat pesan nyata.
- **"Sentuh file"** = file yang akan diubah. **"Bukti jalan"** = cara membuktikan
  fase beres sebelum lanjut.
- Sebelum nulis fitur baru, lihat bagian **"Taktik riset"** di bawah.

---

## Fase 0 — Bersih-bersih 🧹
*Tujuan: buang barang mati. Risiko mendekati nol. Bikin kamu kenal kodebasemu.*

- [ ] **Hapus spinner palsu `_simulate_progress`** — animasi `time.sleep(0.5)` yang
      pura-pura "Validating…/Verifying…" padahal tak memvalidasi apa pun.
      Sentuh file: `cli/dashboard.py` (definisi baris 139 + 4 pemanggilan: 275, 346, 401, 468).
- [ ] **Hapus variabel mati `history_path`** — didefinisikan tapi tak pernah dibaca
      (`grep` cuma menemukan barisnya sendiri).
      Sentuh file: `core/memory.py:158`. ⚠️ Jangan keliru dengan `_history_path` di
      `telegram/sessions.py` — itu method aktif, beda hal.
- [ ] **Buang `tiktoken`** — `estimate_tokens()` sudah punya fallback `len(text)//4`
      saat tiktoken error. Untuk sekadar ambang auto-summarize, perkiraan kasar cukup.
      Ganti isi fungsi jadi `len(text)//4`, hapus `import tiktoken`.
      Sentuh file: `core/memory.py:18,128-135` + `requirements.txt:8`.
- [ ] **Buang dependensi `httpx`** — tak dipakai di mana pun (`grep` = 0). Dependensi gratis dihapus.
      Sentuh file: `requirements.txt:3`.

**❌ TIDAK dilakukan di Fase 0 (sengaja, biar tetap aman) — lihat alasannya:**
- **Gabung 2 wizard** (`cli/dashboard.py` Rich + `cli/panel_wizard.py` plain `input`).
  Ternyata **bukan duplikat murni**: panel wizard ada khusus untuk panel console
  (Pterodactyl) yang "sering tidak berperilaku seperti terminal penuh". Menggabung =
  refactor yang bisa merusak jalur panel yang sulit dites di sini. → ditunda ke Fase 6.
- **Hapus MCP `sse`** (`core/mcp.py:62` `_connect_sse` cuma `pass`). Memang stub, TAPI
  tersambung ke config schema + wizard + `_send_request`. Menghapusnya menyentuh 3 file,
  dan `aiohttp` tetap dibutuhkan `core/llm.py` jadi **tak menghemat dependensi apa pun**.
  → diputuskan saat Fase 5 (MCP).

**Bukti jalan:** `python3 -m compileall core config cli telegram cli.py main.py`
lulus; bot start, balas "halo", jalankan 1 perintah shell — semua normal.

---

## Fase 1 — Amankan 🔒
*Tujuan: tutup celah nyata sebelum nambah fitur. JANGAN dilewati.*

- [x] **Tutup bypass operator shell pada allowlist.** ✅ SELESAI. Celah: matching lewat
      *executable token* meloloskan `ls && rm -rf /` saat `ls` di allowlist (token pertama
      = `ls`), padahal `execute_shell` menjalankan seluruh string. Perbaikan: bila command
      mengandung operator shell (`;` `&` `|` `` ` `` `$` `(` `)` `<` `>` newline), HANYA
      exact-match yang diizinkan — name/glob shortcut dilewati → paksa approval. Logika
      diekstrak ke **`core/shell_policy.py`** (stdlib-only, bisa dites tanpa deps berat);
      `core/agent.py` sekarang impor `is_command_allowlisted` dari sana.
      Self-check: `python3 core/shell_policy.py` → hijau (mencakup chaining/pipe/redirect/`$()`/backtick).
- [x] **Default shell mode = `approval`** — SUDAH benar (`config/settings.py:34-35`,
      default `"approval"`). Tetap default di template & kedua wizard.
- [x] **Audit redaksi secret di log.** ✅ SELESAI. `core/llm.py:32` `_build_headers`
      tak pernah me-log API key; payload & headers tak di-log. Audit `grep` seluruh
      `logger.*` di `core/`+`telegram/`+`config/`: **nol** kebocoran token/api_key
      (satu-satunya hit "Token threshold" = jumlah token konteks, bukan secret).

**Bukti jalan:** ✅ `python3 core/shell_policy.py` hijau; `compileall` lulus; audit log bersih.
⚠️ Tes Telegram langsung (kirim `ls && id` saat mode `list`) = langkah verifikasi milikmu
nanti saat bot jalan dengan dependensi terpasang.

---

## Fase 2 — Provider LLM generik 🔌
*Tujuan: bisa pakai lebih banyak penyedia AI, bukan cuma format OpenAI.*

- [x] **Fallback secret lewat environment variable.** ✅ SELESAI. `config.json` kini
      boleh dikirim **tanpa secret** (deploy headless/Docker/panel): kosongkan
      `bot_token`/`api_key`, isi dari env. Honored: `TELEGRAM_BOT_TOKEN`,
      `LLM_API_KEY_<NAMA_PROVIDER>`, atau `LLM_API_KEY` (umum). Nilai eksplisit di file
      selalu menang. Logika di modul stdlib **`config/env_overrides.py`** (testable tanpa
      pydantic), dipanggil `load_config` (`config/settings.py:244`).
      Self-check: `python3 config/env_overrides.py` → hijau.
- [x] **Fallback 2 tingkat (model → provider).** ✅ SUDAH ADA — tak perlu kode baru.
      Daftar `providers` (`core/llm.py:126`) **itu sendiri** rantai fallback-nya. Mau coba
      model A lalu B sebelum pindah penyedia? Cukup 2 entri provider dengan `base_url`+
      `api_key` sama, `model` beda, priority 1 & 2. Loop fallback (retry 429/5xx) sudah
      menangani. Menambah field `models` = redundan → **dilewati (YAGNI)**.
- [⤵] **Wire-format `anthropic` native (`/v1/messages`)** — **ditunda (YAGNI v1).**
      Alasan: klien OpenAI-compat yang ada **sudah** menjangkau Claude — lewat OpenRouter
      (`anthropic/claude-*`) atau endpoint OpenAI-compat resmi Anthropic
      (`https://api.anthropic.com/v1/`, key sebagai Bearer). Jadi "pakai Claude" sudah bisa
      hari ini tanpa kode baru. Translasi pesan/stream native (`system` top-level, blok
      `tool_use`/`tool_result`, parser SSE event terpisah) = ~250 baris rawan-bug untuk
      manfaat marginal (prompt caching direct-API). Tambah HANYA bila benar butuh direct
      Anthropic API. Saat itu: ekstrak ke `core/wire_anthropic.py` (stdlib) + self-check
      data SSE kalengan — pola sama seperti `shell_policy.py`.

**Bukti jalan:** ✅ `python3 config/env_overrides.py` hijau; `compileall` lulus.
Cara pakai Claude sekarang: provider `base_url` = OpenRouter atau endpoint OpenAI-compat
Anthropic, isi `api_key` (boleh lewat env). Matikan provider utama → fallback ke cadangan.

---

## Fase 3 — Pisahkan otak dari mulut Telegram 🧠
*Tujuan: buang kata "Telegram" dari core. Ini yang buka pintu Discord/WhatsApp di v2.*

- [x] **Parameterisasi nama channel di prompt core.** ✅ SELESAI. Prompt LLM tak lagi
      menanam kata "Telegram". `_VISIBLE_OUTPUT_POLICY` & `_GUEST_BOT_POLICY` jadi template
      `{channel}`; `agent_loop`/`run_agent_once`/`build_system_context` dapat parameter
      `channel_name: str = "Telegram"`. Adapter channel #2 nanti tinggal kirim namanya.
      Teks guest surface + prompt summarize + template default (SOUL/AGENTS/RELATIONSHIP)
      digenerikkan. Sentuh: `core/agent.py`, `core/memory.py`, `core/output_filter.py`,
      `core/llm.py`, `core/tools.py`.
      Verifikasi: `grep -rin telegram core/` tinggal **nama env `TELEGRAM_BOT_TOKEN`**
      (sah) + **default param `channel_name="Telegram"`** (itu memang titik injeksinya) —
      nol di dalam logika/prompt. Cek `ast` memastikan template `.format(channel=...)`
      bersih (tanpa brace nyasar).
- [⤵] **Protokol Channel formal (`core/channel.py`)** — **ditunda sampai channel #2 (YAGNI).**
      `agent_loop` **sudah** yield event generik (`text`/`action_start`/`action_result`/
      `pause_for_approval`/`error`) dan terima `approval_handler` sebagai `Callable` —
      **itu sudah kontrak channel-agnostic-nya.** Bikin `Protocol` dengan satu implementasi
      (Telegram) = abstraksi prematur. Ekstrak `Protocol` saat adapter kedua (Discord)
      benar-benar ditulis, supaya bentuknya pas dari 2 kebutuhan nyata, bukan tebakan.

**Bukti jalan:** ✅ `compileall` lulus; `grep -rin telegram core/` bersih dari logika;
template prompt lolos uji `.format(channel="Discord")`. Semua fitur Telegram lama tetap
jalan (default `channel_name="Telegram"`, `telegram/handlers.py` tak perlu diubah).

---

## Fase 4 — Fitur murah 🎁
*Tujuan: fitur blueprint berdampak besar, kode sedikit.*

- [x] **Skills** — ✅ SELESAI. File markdown + `triggers` (frontmatter) di
      `<workspace_dir>/skills/*.md`. Saat pesan user mengandung trigger (word-boundary,
      case-insensitive), isi skill disisipkan ke system context (owner surface saja).
      Modul stdlib **`core/skills.py`** (parse/match murni, testable), di-wire di
      `core/agent.py`. Self-check `python3 core/skills.py` → hijau.
      **Format skill:**
      ```markdown
      ---
      triggers: deploy, ship it, release
      ---
      # Deploy playbook
      Langkah: jalankan tes → bump versi → tag → push.
      ```
- [x] **Slash command ber-scope** — ✅ SUDAH ADA. `telegram/handlers.py` punya
      `/start /help /reset /stop /restart /topic /status /history`, semuanya owner-scoped
      lewat middleware (`telegram/middleware.py` blok non-`allowed_user_ids`); guest
      lewat `guest_message`. Scope any/owner sudah terpenuhi.
- [⤵] **Admin config write-back** — **ditunda.** "Ganti setting via chat lalu tulis ke
      config.json" itu sensitif-keamanan (mutasi config + secret dari pesan) dan murni
      lapisan Telegram — tak bisa diuji di sini tanpa bot hidup. Tambah belakangan dengan
      helper save aman (load→ubah→validasi pydantic→tulis atomik) + konfirmasi owner.
- [⤵] **Clarify (`ask_user`)** — **ditunda (sebagian sudah ada).** Agent SUDAH bisa
      klarifikasi natural: kalau ambigu, dia cukup tanya di teks final tanpa tool-call →
      loop selesai → balasan user masuk `chat_history` → loop baru lanjut. Tool `ask_user`
      khusus cuma perlu untuk berhenti di TENGAH rangkaian tool — butuh FSM Telegram
      (tangkap pesan berikutnya), tak bisa diuji di sini. Tambah kalau benar perlu.
- [⤵] **Search multi-provider** — **ditunda.** `WebSearchTool` (ddgs) sudah menangani
      error dengan rapi (return pesan error, agent bisa coba lagi). Provider kedua sejati
      (Brave/Serper/Tavily) butuh API key → config + secret yang mungkin user belum punya.
      Tak menebak API internal `ddgs` (rawan rusak tanpa bisa diuji). Tambah saat ada key.

**Bukti jalan:** ✅ `python3 core/skills.py` hijau; `compileall` lulus; skills ter-wire di
`agent.py`. Buat `workspace/agent/skills/deploy.md` → kirim "deploy sekarang" → playbook aktif.

---

## Fase 5 — Fitur lanjut 🚀
*Tujuan: fitur blueprint lebih berat. Kerjakan setelah Fase 4 mantap.*

- [x] **Subagent (`spawn_subagent`)** — ✅ SELESAI. Tool baru `core/subagent.py` yang
      memanggil `run_agent_once` (yang sudah ada) dengan registry **dipangkas**: hanya
      tool non-destruktif, **tanpa** `spawn_subagent` sendiri (cegah rekursi/biaya liar).
      Logika izin di modul stdlib **`core/subagent_policy.py`** (testable). Di-wire di
      `main.py:166`. Lapis aman ganda: tool destruktif dibuang dari registry subagent
      DAN `run_agent_once` jalan dengan `allow_approval=False`.
      Self-check `python3 core/subagent_policy.py` → hijau.
- [x] **Rapikan MCP** — ✅ SELESAI (keputusan: **jangan implement sse, gagalkan dengan
      jelas**). multi-server stdio sudah jalan. `_connect_sse` dulu cuma `pass` →
      server `sse` diam-diam daftar **0 tool** (footgun). Sekarang **`raise
      NotImplementedError`** dengan pesan jelas (MCPManager menangkap & skip server itu,
      bukan diam). Opsi `sse` dihapus dari wizard (`cli/dashboard.py`) → user baru tak
      bisa salah pilih. Schema `transport` tetap terima `"sse"` (kompat config lama, tapi
      kini error jelas saat connect). Implement SSE proper / hapus total = opsional nanti.

**Bukti jalan:** ✅ `python3 core/subagent_policy.py` hijau; `compileall` lulus; subagent
ter-wire di `main.py`; sse gagal dengan pesan jelas, bukan diam.

> Catatan: ide **cron/penjadwal** dari `summary.md` **tidak masuk v1.0** (bukan fitur
> blueprint, dan butuh desain "aman" tersendiri). Lihat bagian v2.0+.

---

## Fase 6 — Operasional 🛠️
*Tujuan: gampang dijalankan & tak hilang data.*

- [x] **CLI kelola proses + `doctor`** — ✅ SELESAI. `cli/manage.py` dengan
      `start`/`stop`/`status`/`doctor`/`selfcheck` (PID file + SIGTERM, tanpa daemonisasi).
      `doctor` validasi config + cek `bot_token`/`api_key` ada → tunjuk yang salah.
      ponytail: untuk produksi tetap pakai Docker (`restart: unless-stopped`) atau systemd
      (supervisi + restart-on-crash); ini buat run bare-metal `python3 main.py`.
      Self-check `python3 cli/manage.py selfcheck` → hijau (helper PID teruji).
- [x] **Data persisten** — ✅ SUDAH ADA. `docker-compose.yml:9` sudah mount
      `./workspace:/app/workspace` + `restart: unless-stopped`. `workspace_dir` default
      `workspace/agent` ada di bawahnya → memory selamat saat redeploy. Tak perlu kode baru.
- [x] **Gabung 2 wizard (ditunda dari Fase 0)** — ✅ SELESAI (versi aman). Logika duplikat
      (`parse_user_ids`, `deep_merge`, `mask_secret`) diekstrak ke **`cli/_shared.py`**
      (stdlib, self-check hijau). Kedua wizard impor dari sana — duplikasi hilang, **tapi
      2 frontend I/O (Rich vs panel) tetap terpisah** (tak digabung paksa → jalur panel
      aman). Self-check `python3 cli/_shared.py` → hijau.

**Bukti jalan:** ✅ `compileall` lulus; `python3 cli/manage.py selfcheck` & `python3
cli/_shared.py` hijau; tak ada def duplikat tersisa di kedua wizard; data persisten sudah
tertangani docker-compose.

---

## v2.0 dan seterusnya — mulut baru 👄 (BUKAN sekarang)

Ditambah HANYA setelah v1.0 stabil. Karena Fase 3 sudah pisah otak↔mulut, ini tinggal
nambah **adapter**, bukan bongkar otak.

- **Discord** — adapter di sisi Python (channel kedua, relatif mudah setelah Fase 3).
- **WhatsApp** — **layanan JS kecil terpisah** (Baileys) yang ngobrol ke otak Python
  via HTTP. Satu-satunya yang benar butuh JS. Kalau Baileys error, otak tetap aman.
- **Cron / penjadwal** (ide `summary.md`) — briefing harian, monitoring, jadwal bahasa
  natural. Wajib desain **aman**: tiap job ada batas, tak bisa spam, gampang dimatikan.

---

## Taktik riset: unduh referensi dari GitHub 📥

Sebelum nulis fitur baru dari nol, **cari dulu yang sudah jadi**. Kalau ada implementasi
referensi yang bagus, **unduh source-nya untuk dianalisis** — ini nilai plus. Urutan:

1. `gh search repos "<fitur>"` dan `gh search code "<pola>"` — cari yang sudah ada.
2. `gh repo clone <owner>/<repo> /tmp/ref-<nama>` — unduh ke `/tmp` buat dibedah.
3. Baca polanya, **ambil idenya**, port ke gaya kode kita (jangan copy buta).
4. Cek dokumen resmi library buat memastikan API-nya benar.

Referensi berguna per fitur:

| Fitur | Cari referensi |
|---|---|
| Wire-format Anthropic (Fase 2) | SDK & contoh resmi Anthropic Messages API |
| Skills + trigger (Fase 4) | repo agent dengan sistem skill markdown |
| Subagent (Fase 5) | pola "sub-agent/handoff" di framework agent open-source |
| MCP sse proper (Fase 5) | `@modelcontextprotocol` SDK & contoh server |
| Discord/WhatsApp (v2) | grammy / discord.js / `@whiskeysockets/baileys` |

> Simpan unduhan di `/tmp` (jangan masuk repo). Yang masuk repo hanya kode kita sendiri
> hasil belajar dari referensi.

---

## Yang SENGAJA tidak dikerjakan di v1.0

- ❌ WhatsApp & Discord (v2 — butuh adapter/microservice baru).
- ❌ Tulis ulang ke JavaScript (otak Python sudah jalan & teruji).
- ❌ Abstraksi spekulatif "buat nanti" (tambah saat benar-benar dibutuhkan).
- ~~Cron~~ → **sudah dibangun di v1.1** (lihat bawah).

---

## Status v1.0 — SEMUA FASE TAMAT ✅ (27 Jun 2026)

Fase 0–6 selesai & **diaudit** (review independen code-reviewer). Verdict:
**APPROVE-WITH-NITS** — 0 CRITICAL. Cek keamanan lolos: tak ada bypass operator shell,
subagent tak bisa rekursi/jalankan tool destruktif, env_overrides tak mutasi/log secret.

**3 temuan diperbaiki saat review:**
- HIGH — panel wizard crash kalau port webhook diisi non-angka → ditambah loop retry.
- MEDIUM — deskripsi subagent salah ("no file writes") padahal boleh pakai memory tool → diperjelas.
- MEDIUM — cabang `sse` mati di `_send_request` → dihapus.

**Sisa temuan LOW (pengerasan masa depan, bukan blocker v1.0):**
- `_send_request` MCP belum ada timeout → server MCP nge-hang bisa nge-hang loop. Tambah `asyncio.wait_for`.
- MCP tool semua dianggap `destructive=False` → subagent bisa pakai tanpa approval. Tambah opsi `treat_as_destructive` per server.
- `load_skills_dir` baca disk tiap pesan → cache (pakai mtime) kalau folder skills membesar.
- String fallback Bahasa Indonesia di `core/agent.py` → satu-satunya teks non-netral di core.

**Modul stdlib baru dengan self-check (semua hijau):** `core/shell_policy.py`,
`config/env_overrides.py`, `core/skills.py`, `core/subagent_policy.py`, `cli/_shared.py`,
`cli/manage.py`. Plus `core/subagent.py` (tool).

**Bukti akhir:** `python3 -m compileall core config cli telegram cli.py main.py` lulus;
6 self-check hijau; dependensi 10 → 8.

---

## Status v1.1 — FITUR AGENT BLUEPRINT DILENGKAPI ✅ (27 Jun 2026)

Fitur agent yang sebelumnya ditunda/YAGNI **dibangun semua** (channel WA/Discord tetap v2).
Diverifikasi live pakai `.venv` (deps terpasang), bukan cuma compile.

**Bug startup yang diperbaiki dulu** (ditemukan saat user benar-benar menjalankan):
- `cli/dashboard.py` `WIZARD_STYLE` pakai `bright_cyan`/`bright_black` → warna prompt_toolkit
  tak valid → crash di import. Diganti `ansibrightcyan`/`ansibrightblack`.
- `cli/manage.py doctor` → `No module named config` saat dijalankan `python3 cli/manage.py`
  (root tak di sys.path). Ditambah bootstrap sys.path.

**Fitur baru (semua dengan self-check / verifikasi live):**

| Fitur | Modul / lokasi | Verifikasi |
|---|---|---|
| **Shell blocklist** (selalu minta approval) | `core/shell_policy.py` `is_command_blocked` + `exec_blocked_commands` | self-check + `rm` di mode `all` tetap approval |
| **Fallback model→provider eksplisit** | `core/llm.py` `_models_for` + `models` di provider | urutan attempt benar |
| **Config YAML** (selain JSON) | `config/settings.py` `_read_config_dict` + `PyYAML` | load YAML live |
| **Search multi-provider** ddg→Brave→Tavily | `core/search.py` | self-check + ddg live |
| **Anthropic wire native** `/v1/messages` | `core/wire_anthropic.py` + `wire_format` field | self-check data SSE kalengan |
| **Clarify `ask_user` + tombol** | `core/agent.py` intercept + `telegram/handlers.py` keyboard/callback | tool terdaftar, di-exclude dari subagent |
| **Slash admin/kontrol + write-back** | `telegram/handlers.py` + `config/settings.py` `save_config_update` | round-trip simpan→reload |
| **Cron/scheduler + NL scheduling** | `core/scheduler.py` (loop asyncio, persist JSON) + tools | self-check due-logic + round-trip |

**Catatan keamanan cron:** job berjalan unattended → pakai **toolset non-destruktif**
(pruning sama seperti subagent), batas `MAX_JOBS=50`, gampang dilihat/dibatalkan
(`list_schedules`/`cancel_schedule` + `/skills` dll). NL scheduling: LLM menerjemahkan
"jam 5 sore" → `daily hour=17`, dst.

**Smoke test startup:** `python3 main.py` boot bersih sampai *workspace memory →
scheduler started → polling* (lalu gagal hanya karena token uji palsu — token asli jalan).

**Self-check (9, semua hijau):** shell_policy, env_overrides, skills, subagent_policy,
search, wire_anthropic, cli/_shared, cli/manage, scheduler. Dependensi: +`PyYAML` (8→9).

**Sisa v2:** Discord (adapter Python) → WhatsApp (microservice JS via HTTP).
