# binance-square-engine

> **AI Growth Engine** that runs 24/7 to drive a Binance Square account into the
> top creators tier — by detecting market signals, generating Arabic viral hooks,
> rendering Binance-style PNG cards, and auto-publishing on a disciplined
> schedule, while learning continuously from engagement results.

---

## ⚠️ Read this first — Risk Disclosure

Automated posting at scale on Binance Square **likely violates Binance Terms of
Service** (the “Automated Access” clauses). The browser-automation publisher in
this repo is included for operators who understand and accept that risk —
possible consequences include posting being rate-limited, the account being
flagged as a bot, or in extreme cases the account being suspended together with
any pending Creator Program earnings.

What this repo deliberately does **not** include:

- Browser fingerprint spoofing (no `playwright-stealth`, no WebGL/Canvas
  randomisation, no audio-context tricks).
- User-agent rotation to fake different devices.
- Anti-detection scripts that mask automation indicators.

What it does include for hygiene:

- Human-paced typing & clicks (random delays + jitter on schedules).
- Hard caps on daily / hourly posts and same-ticker reuse.
- Auto-pause if the algorithm signal degrades.
- A `dry_run` mode that produces every post and image **without** contacting
  Binance — perfect for QA and parameter tuning.

If you prefer to stay on the safe side: keep `PUBLISH_MODE=api` (only the
official `X-Square-OpenAPI-Key` endpoint), lower `MAX_POSTS_PER_DAY` to 5–10,
and review posts manually using `bse recent`.

---

## What the engine does, in one diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                    binance-square-engine                        │
├──────────────┬──────────────┬───────────────┬───────────────────┤
│ Signal       │ Content      │ Visuals       │ Distribution      │
│ Layer        │ Layer        │ Layer         │ Layer             │
│              │              │               │                   │
│ • Binance    │ • Gemini     │ • Playwright  │ • OpenAPI         │
│   24h tickers│   few-shot   │   HTML→PNG    │   publisher       │
│ • Futures    │ • Tendency   │ • 4 native    │ • Browser fallback│
│   movers     │   classifier │   templates   │ • Rate limiter    │
│ • RSS news   │ • Cashtag    │ • ImgBB host  │ • APScheduler     │
│ • Reference  │   resolver   │ • Candlestick │   (20 slots/day)  │
│   feed       │              │   SVG         │                   │
└──────┬───────┴──────┬───────┴───────┬───────┴───────┬───────────┘
       │              │               │               │
       ▼              ▼               ▼               ▼
  Opportunity ──► Post text ──► PNG images ──► Published post
       │                                            │
       │                                            ▼
       │                                    Analytics Layer
       │                              (view/like/comment poll)
       │                                            │
       ▼                                            ▼
  SQLite DB ◄──────── Learning Layer ◄────── Engagement snapshots
                  (template stats, prompt refresh, weekly report)
```

---

## Quick start

### 1. Install

```bash
git clone https://github.com/pinzo435-spec/binance-square-engine.git
cd binance-square-engine

python3.11 -m venv .venv && source .venv/bin/activate
pip install -e .
playwright install chromium
cp .env.example .env
```

### 2. Configure the bare-minimum env vars

Edit `.env` and fill in at least:

```env
GEMINI_API_KEY=...        # https://aistudio.google.com/apikey  (free)
IMGBB_API_KEY=...         # https://api.imgbb.com               (free)
ACCOUNT_HANDLE=KinzoTech  # your handle (appears on cards)
PUBLISH_MODE=dry_run      # start here! switch to api/hybrid later
```

### 3. Initialise

```bash
bse init     # creates the SQLite schema + installs Chromium
bse scan     # one-shot signal scan; prints top 10 opportunities
bse render --ticker BTC --trigger PUMP    # produces a sample PNG card
bse hook --ticker SOL --trigger PUMP      # generates a sample Arabic hook
```

The first two open files end up in `data/runtime/images/`.

### 4. Try a full dry-run slot

```bash
bse run-slot prime_gcc
# logs every step but does NOT actually publish (PUBLISH_MODE=dry_run)
```

### 5. Run the daemon

```bash
bse run                                   # foreground
# or
nohup python auto_publish.py > engine.log 2>&1 &
# or (recommended)
docker compose up -d
```

Then visit the dashboard at <http://localhost:8000>.

---

## How publishing works

`PUBLISH_MODE` in `.env` controls the publisher chain:

| Mode      | Behaviour |
|-----------|-----------|
| `dry_run` | Renders everything, logs the payload, never contacts Binance. **Start here.** |
| `api`     | Only uses the official OpenAPI endpoint (`X-Square-OpenAPI-Key`). Best risk profile but image upload depends on Binance accepting `imageList`. |
| `browser` | Only uses Playwright + saved cookies to drive `binance.com/en/square/post-create`. Survives image limitations but TOS-risky. |
| `hybrid`  | Tries OpenAPI first; falls back to browser on image-related errors. Recommended once you understand both paths. |

### Setting up browser-mode cookies

1. Log into `binance.com` in your normal Chrome.
2. Install a cookie-exporter extension (e.g. *Cookie-Editor*).
3. Export cookies for `binance.com` as JSON (array of objects with
   `name`, `value`, `domain`, `path`, `expires`).
4. Save them:
   ```bash
   bse cookies-import /path/to/binance_cookies.json
   ```
5. The publisher will load these at the start of every browser session.

> Re-export every 1–2 weeks (or whenever Binance signs you out).

---

## CLI reference

```bash
bse init                                  # one-time setup
bse scan                                  # rank opportunities
bse render --ticker BTC --trigger PUMP    # produce sample card
bse hook  --ticker BTC --trigger PUMP     # produce sample hook
bse publish --ticker BTC --hook "$BTC ..." # manual full publish
bse run-slot prime_gcc                    # fire a single slot now
bse run                                   # daemon
bse collect-stats                         # one-off engagement collection
bse report --hours 24 --out report.md     # performance report
bse learn                                 # template stats + prompt refresh
bse recent --limit 20                     # list recent posts
bse pause --hours 2 --reason "maintenance"
bse resume
bse cookies-import ./cookies.json
```

---

## Folder structure

```
binance-square-engine/
├── auto_publish.py                # daemon entry
├── pyproject.toml
├── Dockerfile / docker-compose.yml
├── .env.example
├── playbooks/
│   ├── daily_schedule.yaml        # 20-slot UTC schedule
│   └── burst_triggers.yaml        # extra-burst rules
├── prompts/
│   ├── hook_arabic.txt            # system prompt (Arabic)
│   └── few_shot_examples.json     # 10 few-shot examples
├── data/
│   ├── templates/visuals/         # HTML/CSS card templates
│   └── runtime/                   # SQLite DB + rendered PNGs
├── engine/
│   ├── config.py
│   ├── logging_setup.py
│   ├── db.py / models.py
│   ├── cli.py                     # `bse` CLI (Typer)
│   ├── dashboard.py               # FastAPI live dashboard
│   ├── signal/
│   │   ├── market_scanner.py      # Binance spot + futures
│   │   ├── trend_scraper.py       # Binance Square trends
│   │   ├── news_feed.py           # RSS
│   │   ├── reference_feed.py      # creator profile feed
│   │   └── opportunity_ranker.py  # combines all signals → priority_score
│   ├── content/
│   │   ├── hook_generator.py      # Gemini + sanitiser
│   │   ├── tendency_classifier.py
│   │   ├── cashtag_resolver.py    # ticker → trading pair
│   │   └── post_assembler.py
│   ├── visuals/
│   │   ├── card_renderer.py       # Playwright HTML → PNG
│   │   ├── chart_data.py          # klines fetcher
│   │   ├── pipeline.py            # opportunity → list[PNG]
│   │   └── image_uploader.py      # ImgBB host
│   ├── distribution/
│   │   ├── api_publisher.py       # official OpenAPI
│   │   ├── browser_publisher.py   # Playwright fallback
│   │   ├── rate_limiter.py        # safety rails
│   │   ├── publisher.py           # orchestrator
│   │   └── scheduler.py           # APScheduler
│   ├── analytics/
│   │   ├── post_tracker.py        # poll engagement
│   │   └── reports.py
│   └── learning/
│       ├── template_evaluator.py
│       └── prompt_updater.py
└── tests/
```

---

## Technical decisions

| Concern | Choice | Why |
|---|---|---|
| Async I/O | `httpx` + `asyncio` | Many concurrent HTTP calls (market scan, news, klines). |
| Retries | `tenacity` | Battle-tested with explicit backoff windows. |
| LLM | Gemini 1.5 Flash | Free tier sufficient for ~10 hooks/min; provider-agnostic abstraction allows Claude/OpenAI swap. |
| Templates | Plain HTML/CSS rendered by Playwright | Pixel-perfect Binance look, easy to iterate, no design pipeline. |
| Images host | ImgBB | Free, public CDN URLs that the OpenAPI accepts. |
| Schedule | APScheduler + YAML playbook | Editable without touching code; jitter per slot. |
| DB | SQLite via SQLAlchemy 2.0 async | Single-binary deploy, ergonomic ORM, easy to upgrade to Postgres later. |
| CLI | Typer | Type-hinted, modern, friendly errors. |
| Dashboard | FastAPI + tiny vanilla JS | Zero-build, embeddable, easy on a VPS. |
| Logging | structlog | Console-friendly in dev, JSON in prod. |
| Containerisation | Single Docker image, two compose services | Engine + Dashboard share the image but run as separate processes. |

---

## Execution plan (recommended onboarding)

| Day | Action |
|---|---|
| 0   | `bse init` → run in `dry_run` mode for 24 h. Read every generated post. |
| 1   | Tune `playbooks/daily_schedule.yaml` to taste (start with 8 slots, not 20). |
| 2   | Add hooks of your own to `prompts/few_shot_examples.json`. |
| 3   | Flip `PUBLISH_MODE=api`. Set `MAX_POSTS_PER_DAY=10`. Watch the dashboard. |
| 7   | Use `bse report --hours 168 --out week1.md` to find the best templates / hours. |
| 14  | Run `bse learn` to fold top performers into the few-shot pool. |
| 30  | Increase posts/day gradually as engagement clears the `LOW_VIEWS_THRESHOLD`. |
| 60  | Add a second template variant per category (A/B test). |
| 90  | Apply for Verified Creator inside the Binance Square app. |

KPI targets (from strategy doc §12.1):

| Month | Posts/day | Followers | Avg views/post |
|---|---:|---:|---:|
| 1 | 8 | 200–500 | 100–500 |
| 2 | 15 | 800–2,000 | 800–2,000 |
| 3 | 25 | 3,000–8,000 | 2,500–5,000 |

---

## Strategy reference

This engine was designed against `binance_square_growth_strategy.md` — a deep
reverse-engineering of `momomomo7171` (45.2k followers, 11.9M views, 3,657 posts
analysed). Every behavioural rule in the engine maps to a measured insight:

- **`$TICKER` bookends** in `_sanitise()` ← 86% of top posts open with cashtag.
- **60–250 char body** ← median 63, optimal band 60–250.
- **0–2 emoji**, no `!` ← 0.45 emojis/post, 0.01 `!`/post empirically.
- **20-slot UTC schedule** ← Asia/EU/US/GCC volume curves.
- **Same-ticker 4 h gap** ← cadence guard cited in safety rails.
- **Bear-leaning template mix** ← bearish posts measured at +15% views.

---

## License

MIT.
