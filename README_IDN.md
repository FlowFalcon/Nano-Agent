# Nano-Agent

> AI agent ramah pemula yang hidup di chat Telegram-mu.

![Python](https://img.shields.io/badge/python-3.12+-blue)
![Status](https://img.shields.io/badge/status-alpha-orange)
![License](https://img.shields.io/badge/license-MIT-green)

🇬🇧 *English version: [README.md](README.md)*

Nano-Agent adalah AI agent yang kamu hosting sendiri dan kamu ajak ngobrol lewat
Telegram. Dia bisa menjalankan perintah shell, baca/tulis file, cari & ambil isi web,
mengingat hal tentangmu, menjadwalkan tugas, dan memakai tool eksternal lewat MCP —
semua dengan **persetujuan (approval)** untuk hal berisiko. Bisa pakai LLM apa pun yang
kompatibel format OpenAI atau Anthropic.

**Kenapa ramah:**

- 🛡️ **Approval dulu + alasan + "izinkan sesi ini"** — aksi berbahaya minta izin, dengan penjelasan *kenapa*, plus opsi izinkan sekali atau sepanjang sesi.
- 🧠 **Memori menetap** — ingat fakta tentangmu & proyekmu dalam file Markdown yang bisa kamu edit.
- 🧩 **Upload file** — kirim dokumen, gambar, PDF, atau ZIP — agent simpan & baca sendiri.
- 🧠 **Self‑aware** — agent tahu tool, config, dan dokumennya sendiri. Tanya *"apa saja yang bisa kamu lakukan?"* untuk daftar kemampuan langsung.
- 💬 **Ikut bahasamu** — menu/UI berbahasa Inggris, tapi agent membalas dalam bahasa yang kamu pakai.

---

## Mulai Cepat

```bash
# 1. Clone & masuk folder
git clone <url-repo-mu> nano-agent && cd nano-agent

# 2. Pasang dependency (pakai virtualenv)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Jalankan wizard setup (minta bot token + API key LLM)
python cli.py

# 4. Nyalakan bot
python main.py
```

Kamu butuh **token bot Telegram** dari [@BotFather](https://t.me/BotFather) dan **API key
LLM** (OpenRouter, OpenAI, Anthropic, Groq, dll). Lalu chat botmu.

> Belum ada config? `python main.py` otomatis membuka wizard yang sama.

---

## Konfigurasi

Config ada di `config.json` (atau `config.yaml`). Secret juga bisa dari environment,
jadi tak perlu ditulis di file.

Isi secret lewat env (kosongkan di file):

| Variable | Mengisi |
|----------|---------|
| `TELEGRAM_BOT_TOKEN` | `telegram.bot_token` |
| `LLM_API_KEY` | `api_key` provider mana pun |
| `LLM_API_KEY_<NAMA_PROVIDER>` | `api_key` provider itu (mis. `LLM_API_KEY_OPENROUTER`) |
| `BRAVE_API_KEY`, `TAVILY_API_KEY` | cadangan web-search (opsional) |

**Pakai Claude?** Set `base_url` provider ke OpenRouter atau endpoint OpenAI-compatible
Anthropic (default `wire_format: openai`), atau pakai `wire_format: anthropic` untuk
API `/v1/messages` langsung.

**Fallback 2 tingkat:** tiap provider mencoba semua model di `models` dulu, baru pindah
ke provider berikutnya.

---

## Cara Kerja

```
Telegram ──▶ handlers ──▶ agent loop ──▶ provider LLM
                │             │  ▲
                │             ▼  │ panggilan tool (lewat approval)
            sessions     tool registry ──▶ tool bawaan + server MCP
            (riwayat          │
             per-topik)       ▼
                        workspace memory (otak Markdown + catatan harian)
```

- **Agent loop** (`core/agent.py`) men-stream balasan model, menjalankan tool yang diminta,
  lalu memasukkan hasilnya kembali sampai task selesai — dibatasi `max_iterations`.
  Chat panjang **diringkas otomatis** saat melewati ambang token.
- **Tool registry** (`core/tools.py`) menyediakan tool bawaan; server MCP dan tool
  sub-agent ikut terdaftar di registry yang sama, jadi semua jadi satu set tool.
- **Approval** (`core/shell_policy.py`, kebijakan subagent) menahan hal berisiko sebelum
  jalan, lengkap dengan alasan dan memori "izinkan sesi ini".
- **Workspace memory** (`core/memory.py`) adalah otak Markdown — dimuat ke konteks tiap
  giliran, diperbarui sambil agent belajar.
- **Deteksi runtime** (`core/runtime.py`) mengenali host VPS, container, panel Pterodactyl,
  dan serverless, lalu menyesuaikan setup awal dan instalasi browser.
- **Sesi per-topik** (`telegram/sessions.py`) menyimpan riwayat terpisah per topik/thread
  Telegram, jadi percakapan paralel tak saling tercampur.

---

## Tools

Tool bawaan agent (MCP menambah lebih banyak):

- **File:** `read_file`, `write_file`, `replace_in_file`, `list_files`, `search_files`, `make_directory`, `delete_file`
- **Web:** `web_search` (DuckDuckGo → Brave → Tavily), `fetch_url`
- **Sistem:** `execute_shell`, `get_current_time`
- **Memori:** `update_user_fact`, `update_project_memory`, `forget_memory`
- **Interaksi:** `ask_user` (tanya balik pakai tombol)
- **Lanjut:** `spawn_subagent`, `schedule_task` / `list_schedules` / `cancel_schedule`

Yang berbahaya (`execute_shell`, tulis/hapus file) minta approval dulu.

### Dukungan Browser (opsional)

Wizard setup bisa memasang **Playwright + Chromium** untuk tool web/screenshot.
Instalasinya **menyesuaikan lingkungan**:

- **VPS / container / lokal** — memasang wheel Playwright dan binari Chromium (plus
  library OS yang dibutuhkan bila jalan sebagai root), lalu memverifikasi bisa dijalankan.
- **Panel Pterodactyl / serverless** — **otomatis dilewati** dengan pesan jelas, karena
  host seperti itu biasanya tak bisa menjalankan browser. Kamu dapat perintah siap-tempel
  untuk dicoba nanti bila host-mu mendukung.

Bisa dipasang kapan saja lewat wizard; tidak ada fitur lain yang bergantung padanya.

---

## Skills & Memori

**Skills** = panduan Markdown kecil yang otomatis dimuat saat pesanmu mengandung kata
pemicu. Taruh di `workspace/agent/skills/*.md`:

```markdown
---
triggers: deploy, rilis
---
# Panduan Deploy
Jalankan tes → naikkan versi → tag → push.
```

**Memori** = "otak" berupa file Markdown di `workspace/agent/` yang dimuat agent ke
konteksnya tiap giliran:

| File | Isi |
|------|-----|
| `IDENTITY.md` / `SOUL.md` | Siapa agent ini & cara dia bersikap |
| `USER.md` | Fakta tetap tentangmu |
| `RELATIONSHIP.md` | Cara kalian bekerja sama — gaya, preferensi, koreksi |
| `MEMORY.md` | Keputusan proyek & catatan jangka panjang |
| `AGENTS.md` | Aturan operasi agent |
| `memory/YYYY-MM-DD.md` | Catatan harian (episodik) |

Semua file ini bisa kamu edit manual; agent membaca & memperbaruinya sambil belajar.

### Membaca workspace saat start

Saat boot, agent memindai folder workspace-nya dan menyisipkan **snapshot langsung**
(file, ukuran, folder) ke konteksnya, jadi dia tahu apa yang sudah disimpan dan apa
yang bisa dipakai — bukan cuma prompt statis.

### Berkembang seiring dipakai

Di batas sesi (`/new`, atau saat chat panjang diringkas otomatis) agent menjalankan
**satu refleksi singkat** atas percakapan lalu menuliskan yang dipelajari: fakta baru ke
`USER.md`, catatan gaya kerja ke `RELATIONSHIP.md`, dan keputusan ke `MEMORY.md`. Lama-lama
dia makin pas denganmu tanpa perlu kamu atur.

---

## Penjadwalan

Cukup minta pakai bahasa natural:

> "Ingatkan aku cek email tiap hari jam 5 sore."

Agent mendaftarkan job yang jalan otomatis (dengan tool aman/non-destruktif) dan
mengirim hasilnya ke kamu. Maks 50 job, gampang dilihat/dibatalkan.

---

## Slash Command

Menu perintah sengaja dibuat kecil — yang penting saja plus kontrol admin khusus owner.

| Perintah | Fungsi |
|----------|--------|
| `/help` (& `/start`) | Bantuan & daftar perintah |
| `/new` | Mulai sesi baru — sekaligus menjalankan refleksi memori (lihat di bawah) |
| `/stop` | Hentikan task yang sedang berjalan |
| `/restart` | Restart proses bot |
| `/status` | Lihat status agent (model, mode, tools, job) |
| `/topic <nama>` | Buat/pindah topik Telegram (tip; tak ditampilkan di menu) |
| `/model [m]` | **Admin** — lihat/ganti model utama |
| `/shell [ask\|list\|all]` | **Admin** — lihat/ganti mode keamanan shell |
| `/allow [cmd]` | **Admin** — lihat/tambah allowlist shell |
| `/block [cmd]` | **Admin** — lihat/tambah blocklist shell |
| `/mcp` | **Admin** — daftar server MCP |
| `/skills` | **Admin** — daftar skill aktif |

Perintah admin khusus owner dan langsung tersimpan ke config.

---

## Keamanan & Approval

Mode shell (`/shell`):

- **ask** (default) — tiap perintah minta izin.
- **list** — perintah di allowlist langsung jalan; sisanya minta izin.
- **all** — semua jalan tanpa tanya (hati-hati).

**Blocklist** (`/block`) selalu memaksa approval untuk perintah cocok — bahkan di mode
`all` (mis. blokir `rm`, `shutdown`). Allowlist tahan bypass operator: `ls && rm -rf /`
tak akan lolos lewat entri `ls`.

---

## Jalan di Produksi

- **Docker** (disarankan): `docker compose up -d`. Sudah mount `./workspace` (memori
  menetap) + `restart: unless-stopped`.
- **Bare-metal**: `python cli/manage.py start | stop | status | doctor`.
- **Pterodactyl / panel**: wizard mendeteksi panel console dan pakai pilihan A/B/C.

`python cli/manage.py doctor` mengecek config & memastikan secret ada.

---

## Masalah Umum

| Gejala | Solusi |
|--------|--------|
| Wizard/CLI tak jalan | `pip install -r requirements.txt` di dalam venv |
| "Web search unavailable" | DuckDuckGo membatasi IP VPS — set `BRAVE_API_KEY` / `TAVILY_API_KEY` |
| Error config saat boot | `python cli/manage.py doctor` |
| Bot diam | Cek token & pastikan user ID-mu ada di `allowed_user_ids` |

---

## Lisensi

MIT. Lihat `LICENSE`.
