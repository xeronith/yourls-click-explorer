# 🔗 YOURLS Click Explorer

A fast, friendly terminal UI for exporting **lifetime click counts** from a self-hosted [YOURLS](https://yourls.org/) instance.

Look up individual short links, export filtered batches, or scan everything — with a live progress display, a summary panel, and clean CSV/JSON output.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Rich](https://img.shields.io/badge/UI-rich-cyan)
![YOURLS](https://img.shields.io/badge/API-YOURLS-green)

---

## Table of contents

- [Features](#-features)
- [Quick start](#-quick-start)
- [Configuration](#-configuration)
- [Usage](#-usage)
- [All options](#-all-options)
- [How it works](#-how-it-works)
- [Important limitations](#-important-limitations)
- [Troubleshooting](#-troubleshooting)
- [Project layout](#-project-layout)
- [Security notes](#-security-notes)

---

## ✨ Features

| Feature | What it does |
|---|---|
| **Direct lookup** | Look up one or many exact short links without paging: `./run.sh abc def` |
| **Full export** | Page through all links and write CSV or JSON |
| **Creation-date filters** | `--days`, `--created-after`, `--created-before` |
| **Click-count filters** | `--min-clicks`, `--max-clicks` |
| **Text search** | Match keyword, short URL, destination URL, or title |
| **Live UI** | Progress, elapsed time, summary panel, top-links table |
| **Missing links** | 404s are reported in a “Not found” panel instead of aborting |
| **`.env` support** | Keep the API URL and token out of your shell history |
| **`run.sh`** | Creates/activates the virtualenv and installs Rich automatically |
| **Partial save** | `Ctrl+C` writes the links fetched so far |

---

## 🚀 Quick start

```bash
cd /path/to/YOURLS

# First time only: create your local config
cp .env.example .env

# Edit .env and set YOURLS_API_URL and YOURLS_TOKEN

./run.sh --help
./run.sh --days 30 --output last_30_days.csv
```

Your `.env` should look like this:

```dotenv
YOURLS_API_URL=https://your-short-domain/yourls-api.php
YOURLS_TOKEN=your-signature-token
```

Find the token in the YOURLS admin area:

> **Tools → Secure passwordless API call → signature token**

The API URL is the `yourls-api.php` file at the root of your YOURLS installation. If YOURLS lives in a subdirectory, include it in the URL.

### Manual installation

`run.sh` handles this for you, but you can also do it manually:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install rich
python yourls_cli.py --help
```

---

## ⚙️ Configuration

### `.env` variables

| Variable | Required | Description |
|---|---|---|
| `YOURLS_API_URL` | Yes | Full URL to `yourls-api.php` |
| `YOURLS_TOKEN` | Yes | Signature token from the YOURLS Tools page |
| `YOURLS_OUTPUT` | No | Default output file (default: `yourls_clicks.csv`) |
| `YOURLS_PAGE_SIZE` | No | Links per API request (default: `200`) |

The script looks for `.env` in:

1. The current working directory
2. The directory containing `yourls_cli.py`

You can also point to a specific file:

```bash
./run.sh --env-file /path/to/other.env --days 30
```

### Precedence

```text
CLI flags  >  shell environment  >  .env file  >  built-in defaults
```

So this:

```bash
YOURLS_TOKEN=from-shell ./run.sh --days 30
```

uses `from-shell` even if `.env` contains a different token.

---

## 🖥️ Usage

### Look up specific links (no paging)

Pass bare keywords or full short URLs as positional arguments:

```bash
./run.sh abc
./run.sh abc def ghi
./run.sh https://your-short-domain/abc
```

Or use `--shorturl`, which can be repeated or comma-separated:

```bash
./run.sh --shorturl abc,def
./run.sh --shorturl abc --shorturl def
```

Direct mode uses YOURLS’ `url-stats` action, so it makes **one request per link** and never pages through the full database.

Example summary:

```text
Requested  3
Found      2
Missing    1
Clicks     2,773
```

Missing links are listed separately:

```text
Not found (1)
nope
```

Existing filters still apply to the looked-up links:

```bash
# Only include these links if they have at least 1,000 clicks
./run.sh --min-clicks 1000 abc def ghi

# Only include these links if they were created in the last 30 days
./run.sh --days 30 abc def
```

### Export all links

```bash
./run.sh --output all_clicks.csv
```

This pages through the YOURLS `stats` API and writes one row per link.

### Filter by link creation date

These filters use `yourls_url.timestamp` — **when the short link was created**, not when it was clicked.

```bash
# Links created in the last 30 days
./run.sh --days 30 --output last_30_days.csv

# A specific date range
./run.sh \
  --created-after 2026-10-01 \
  --created-before 2026-10-31 \
  --output october.csv
```

Dates accept:

```text
YYYY-MM-DD
YYYY-MM-DD HH:MM:SS
```

`--created-before 2026-10-31` includes the whole day. Timezone offsets are not supported; use the YOURLS server’s local time.

> `--created-after` and `--days` can stop paging early because YOURLS returns newest links first. `--created-before` alone still scans the full set.

### Filter by clicks

```bash
# High-traffic links only
./run.sh --min-clicks 100000 --output top_links.csv

# Low-traffic links
./run.sh --max-clicks 10 --output low_traffic.csv

# A range
./run.sh --min-clicks 1000 --max-clicks 10000 --output mid_traffic.csv
```

Click filters can also stop early: high-click queries use YOURLS’ `top` sort, and low-click queries use `bottom`.

### Search by text

```bash
./run.sh --search example --min-clicks 1000 --output search_results.csv
```

`--search` is case-insensitive and matches across:

- `keyword`
- short URL
- destination URL
- title

`--search` is applied as rows arrive, so it must scan the link set before it knows what matches. Pass exact links as positional arguments when you already know them.

### Output formats

CSV is the default:

```bash
./run.sh --days 30 --output last_30_days.csv
```

JSON:

```bash
./run.sh --format json --output clicks.json
```

CSV or JSON on stdout:

```bash
./run.sh --format json --output - > clicks.json
```

The terminal UI is written to **stderr**, so stdout stays clean for piping.

Quiet mode for cron jobs and scripts:

```bash
./run.sh --quiet --days 30 --output last_30_days.csv
```

### Sample output

```text
╭────────────────────────────────────────────────╮
│  🔗  YOURLS Click Explorer                     │
│  Every short link, every lifetime click count  │
│                                                │
│  API  https://your-short-domain/yourls-api.php │
│  Filter  created >= 2026-10-12 00:00           │
│  Ctrl+C saves partial results                  │
╰────────────────────────────────────────────────╯

⠸ Page 1 · scanned 200 · matched 111  0:00:00

╭────────────────────────────── Export complete ───────────────────────────────╮
│           Filter  created >= 2026-10-12 00:00                                │
│          Scanned  200                                                        │
│          Matched  111                                                        │
│       Stopped at  date cutoff                                                │
│           Clicks  48,392                                                     │
│ Zero-click links  17                                                         │
│   Average clicks  436.0                                                      │
│          Elapsed  2.4s                                                       │
│           Output  last_30_days.csv (CSV)                                     │
╰──────────────────────────────────────────────────────────────────────────────╯

                             Top 10 links by clicks
╭─────┬──────────────┬────────┬────────────────────┬───────────────────────────╮
│   # │ Keyword      │ Clicks │ Share              │ Destination               │
├─────┼──────────────┼────────┼────────────────────┼───────────────────────────┤
│   1 │ campaign-000 │  1,410 │ ██████████████████ │ https://example.com/...   │
│   2 │ campaign-001 │  1,363 │ █████████████████  │ https://example.com/...   │
╰─────┴──────────────┴────────┴────────────────────┴───────────────────────────╯
```

### CSV columns

```csv
keyword,shorturl,longurl,title,created_at,created_ip,clicks
abc,https://your-short-domain/abc,https://example.com/page,Page title,2026-01-01 10:00:00,1.2.3.4,42
```

| Column | Meaning |
|---|---|
| `keyword` | The `abc` part of `https://your-short-domain/abc` |
| `shorturl` | Full short URL |
| `longurl` | Destination URL |
| `title` | Page title |
| `created_at` | Link creation timestamp |
| `created_ip` | IP that created the link |
| `clicks` | Lifetime click counter |

---

## 🎛️ All options

| Option | Default | Description |
|---|---|---|
| `SHORTURL ...` | — | One or more short links/keywords to look up directly |
| `--shorturl SHORTURL` | — | Same as positional; repeatable or comma-separated |
| `--env-file PATH` | auto | Load configuration from a specific `.env` file |
| `--api-url URL` | `.env` | YOURLS API endpoint |
| `--token TOKEN` | `.env` | YOURLS signature token |
| `--output PATH` | `yourls_clicks.csv` | Output file, or `-` for stdout |
| `--format {csv,json}` | `csv` | Output format |
| `--page-size N` | `200` | Links per API request in scan mode |
| `--created-after DATE` | — | Links created on/after this date |
| `--created-before DATE` | — | Links created on/before this date |
| `--days N` | — | Links created in the last N days |
| `--min-clicks N` | — | Minimum lifetime clicks |
| `--max-clicks N` | — | Maximum lifetime clicks |
| `--search TEXT` | — | Case-insensitive text search |
| `--top N` | `10` | Rows in the top-links table; `0` hides it |
| `--no-color` | off | Disable colors |
| `--quiet` | off | Hide the UI; only data/errors |
| `-h`, `--help` | — | Show help |

---

## 🔍 How it works

| Mode | YOURLS API action | Behavior |
|---|---|---|
| **Direct lookup** | `url-stats` | One request per given link; no paging |
| **Scan** | `stats` | Pages with `limit` + `start` |

In scan mode:

- YOURLS `filter=last` returns newest links first.
- `--created-after` and `--days` stop as soon as the page reaches the date cutoff.
- `--min-clicks` uses `filter=top`; `--max-clicks` uses `filter=bottom`; both can stop early.
- `--created-before` and `--search` still scan all pages, then filter locally.

> When `--created-after` or `--days` is also present, date paging takes precedence; click filters are then applied to the matching rows.

All click numbers come from YOURLS’ lifetime `clicks` counter. The tool does **not** compute per-day click counts.

---

## ⚠️ Important limitations

### Lifetime totals only

The core YOURLS API exposes **lifetime click totals**, not historical click events.

This means:

- ✅ “How many clicks does link `abc` have in total?”
- ✅ “Which links were created in October?”
- ❌ “How many clicks happened between October 1 and October 31?”
- ❌ “Show me a daily click chart for link `abc`.”

For click-date analytics, use the `yourls_log` table directly (or a YOURLS plugin that exposes it).

### Date filters are creation dates

`--days`, `--created-after`, and `--created-before` filter `yourls_url.timestamp` — the moment the short link was created.

They do **not** filter click time.

### Timezone

YOURLS returns timestamps without a timezone. Comparisons use the YOURLS server’s local time. If you pass a timezone offset such as `2026-10-01T00:00:00+03:30`, the tool rejects it.

### Bot traffic

Click counts may include bots, scanners, and link previews, depending on your YOURLS configuration and plugins.

### API rate and load

Direct lookup sends one request per link. For very large batches, scan mode with filters may be kinder to your server.

---

## 🧰 Troubleshooting

| Problem | Fix |
|---|---|
| `python3: command not found` | Install Python 3.10+ and make sure it is on `PATH` |
| `This tool needs Rich` | Run `./run.sh` once, or `pip install rich` |
| `API error: ...` | Check `YOURLS_API_URL`, `YOURLS_TOKEN`, and that the API is reachable |
| `Invalid signature` / auth error | Regenerate the token from YOURLS → Tools; do not use the admin password |
| Missing links | Expected for unknown keywords; they appear in the “Not found” panel |
| Empty CSV | Filters are too narrow; check dates, `--min-clicks`, and `--search` |
| `.env` ignored | Put it next to `yourls_cli.py`, or pass `--env-file` |
| `Permission denied: ./run.sh` | `chmod +x run.sh` |
| Dates look shifted | Use the YOURLS server’s local date/time; timezone offsets are unsupported |
| Output overwritten | Use `--output` with a different filename for filtered exports |

---

## 📁 Project layout

```text
YOURLS/
├── yourls_cli.py       # Main CLI
├── run.sh              # Venv bootstrap + activation wrapper
├── .env                # Local config, including the API token (not committed)
├── .env.example        # Configuration template
├── .gitignore          # Keeps secrets, venvs, and exports out of git
├── README.md           # This file
├── .venv/              # Created by run.sh (not committed)
└── yourls_clicks.csv   # Default output (not committed)
```

---

## 🔐 Security notes

- `.env` contains your API token. **Do not commit it.**
- Use HTTPS for `YOURLS_API_URL`.
- Treat the signature token like a password. Anyone with it can make API requests as that user.
- The included `.gitignore` excludes `.env`, `.venv/`, and CSV exports. Keep it that way.

---

## 🧪 Quick reference

```bash
# One link
./run.sh abc

# Many links
./run.sh abc def ghi
./run.sh --shorturl abc,def

# Last 30 days
./run.sh --days 30 --output last_30_days.csv

# October 2026, at least 100 clicks
./run.sh \
  --created-after 2026-10-01 \
  --created-before 2026-10-31 \
  --min-clicks 100 \
  --output october.csv

# High-click links
./run.sh --min-clicks 100000 --output top_links.csv

# Search
./run.sh --search example --min-clicks 1000

# JSON on stdout
./run.sh --format json --output - > clicks.json
```

---

Made for quick, scriptable access to YOURLS click data without opening the admin panel.
