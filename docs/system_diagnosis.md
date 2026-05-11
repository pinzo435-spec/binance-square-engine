# binance-square-engine — System Diagnosis & Roadmap

**Author:** Lead Engineer (Devin)  
**Date:** 2026-05-11  
**Repo HEAD:** main (post-merge of PR #1, PR #2)  
**Code size:** ~3,900 LOC Python across 6 layers + 4 visual templates + 4 tests + 16 unit tests passing

This document is the ground-truth audit of the current state of the engine,
followed by a concrete, ordered roadmap to take it from "working MVP" to
"production-grade growth engine" per the SOURCE-OF-TRUTH strategy document
(`binance_square_growth_strategy.md`).

---

## 1. Current Project Status

| Layer        | Status      | Quality | Verdict                                                          |
|--------------|-------------|---------|------------------------------------------------------------------|
| Signal       | **Working** | B+      | Market scanner + news = enough fuel. Trending + reference broken.|
| Content      | **Working** | A-      | Gemini REST + fallback chain + sanitiser proven on real tickers. |
| Visuals      | **Working** | B+      | 4 templates render correctly; ImgBB uploads working.             |
| Distribution | **Partial** | C       | API path untested live, browser path has speculative selectors.  |
| Analytics    | **Blocked** | C-      | Depends on reference_feed which is broken.                       |
| Learning     | **Idle**    | B       | Code is correct, but has no engagement data to learn from yet.   |
| Operator UX  | **Working** | A-      | CLI (`bse`), dashboard, auto_publish.py daemon all functional.   |

**Bottom line:** the pipeline produces valid Arabic posts + branded PNG images
end-to-end in `dry_run`. The two remaining hard problems are:
1. Native image posting on Binance Square (browser publisher needs real
   selectors + cookies).
2. Engagement harvesting (reference_feed schema regression).

Everything else is incremental polish.

---

## 2. What Works (validated)

### Signal Layer
- **`market_scanner.py`** — Pulls 24h tickers from `api.binance.com` spot
  & USD-M futures, filters by `quote_volume >= 1.5M`, returns top 80.
  Verified: 80 returns/scan, 12s typical latency.
- **`news_feed.py`** — Cointelegraph + CoinDesk RSS, parses headline +
  cashtags + timestamp. Verified: 55 items/scan.
- **`opportunity_ranker.py`** — Score formula:
  `base_volume_score + change_24h_bucket + trend_match_bonus +
   news_coverage_bonus`. Triggers: `EXTREME_PUMP` (≥25%), `PUMP` (≥8%),
  `DUMP` (≤-15%), `STEADY` (else). Verified: produces scores 4–7 with
  the expected distribution.

### Content Layer
- **`hook_generator.py`** — REST call to `generativelanguage.googleapis.com/v1beta`
  with a fallback chain
  (`gemini-2.5-flash → gemini-2.0-flash → gemini-2.5-flash-lite`).
  Sanitiser enforces 30–280 chars, $TICKER bookends, ≤2 emojis, no `!`.
  Few-shot fallback when API exhausted. Verified on 6 tickers:
  ```
  $OSMO انفجرت انفجار يا الربع من امس واليوم +186% 🤑 $OSMO
  $SUI يا جماعة وش صاير بالـ SUI أمس داخلين بسعر واليوم شي ثاني 🤑 $SUI
  $MBL انفجرت انفجار تاريخي بعد صعود +27% خلال يوم واحد 🔥 $MBL
  ```
- **`cashtag_resolver.py`** — Maps ticker → likely Binance trading pair
  (e.g. `BTC` → `BTCUSDT`) using cached `exchangeInfo`.
- **`tendency_classifier.py`** — Trigger → tendency mapping (1 = bullish,
  2 = bearish, 0 = neutral).
- **`post_assembler.py`** — Glues hook + pair + tendency into an
  `AssembledPost`.

### Visuals Layer
- **`card_renderer.py`** — Playwright renders HTML templates → PNG
  (1440×3088 vertical, Binance dark theme). 4 templates working:
  `trade_card`, `chart_card`, `profit_explosion`, `warning_card`.
- **`chart_data.py`** — Fetches 48× 1h klines from Binance, formats SVG
  candles for `chart_card`.
- **`image_uploader.py`** — ImgBB upload via base64 (12/12 successful in
  the dry-run window).
- **`pipeline.py`** — Orchestrates render → upload → returns paths +
  URLs.

### Distribution Layer
- **`rate_limiter.py`** — DB-backed: enforces 40/day, 4/hour, 4h gap per
  ticker, auto-pause after 3 low-view posts. Verified by unit tests.
- **`scheduler.py`** — APScheduler with 20 CronTrigger slots + 5-min
  `refresh_signals` IntervalTrigger + 1-hour `maintenance`. Verified
  running 25+ min stable.
- **`publisher.py`** (the orchestrator) — Routes by `publish_mode`:
  `api`, `browser`, `hybrid`, `dry_run`. Verified working in `dry_run`
  end-to-end.

### Operator surface
- **CLI** (`bse init|scan|render|hook|publish|run|run-slot|report|learn|pause`)
  — All sub-commands working in our window.
- **FastAPI dashboard** (`/`, `/api/posts/recent`, `/api/opportunities`,
  `/api/report`, `/docs`, `/api/pause`, `/api/resume`) —
  Verified live via public tunnel.
- **`auto_publish.py`** — Long-running daemon entry point.
- **Database** (SQLite, async via `aiosqlite`) — All migrations
  auto-apply on `bse init`. 4 models: `Opportunity`, `Post`,
  `EngagementSnapshot`, `Template`, `PublishLock`.

---

## 3. What Is Broken

### 3.1 Reference Feed — schema regression
- **File:** `engine/signal/reference_feed.py`
- **Symptom:** `POST .../queryUserProfilePageContentsWithFilter` returns
  HTTP 200 but body `{"code":"000002","message":"illegal parameter"}`.
  Tested 7 payload variants (`targetSquareUid`, `squareUid`, `userId`,
  `targetUid`, with/without `filterType`, etc.) — all rejected.
- **Impact:** Blocks **PostTracker** (analytics) and **few-shot
  refresh** (learning) and **reference-style mining**.
- **Root cause hypothesis:** Endpoint now requires (a) a CSRF token
  from a logged-in session and/or (b) a different request shape that
  matches what the new Binance Square SPA sends after their late-2025
  redesign.
- **Severity:** HIGH — without engagement data, the Learning Layer can't
  do its job.

### 3.2 Binance Square Trending — endpoints all 404
- **File:** `engine/signal/trend_scraper.py`
- **Symptom:** All 3 candidate URLs in `TREND_ENDPOINTS` return 404.
- **Impact:** No "trending hashtag" bonus in scoring → opportunities are
  ranked purely on price/volume + news, missing the trend-jacking edge.
- **Severity:** MEDIUM — the engine still functions but loses one of
  the strategy doc's pillars.

### 3.3 Browser Publisher — selectors are speculative
- **File:** `engine/distribution/browser_publisher.py`
- **Symptom:** SELECTORS dict contains educated guesses
  (e.g. `'div[contenteditable="true"][data-placeholder]'`) but has never
  been validated against the live DOM with a logged-in session.
- **Impact:** Until cookies are imported and a single dry-publish runs
  against the real UI, we don't know which selectors actually match.
- **Severity:** HIGH — this is the difference between "working bot" and
  "broken bot".

### 3.4 API Publisher — never run with a real key
- **File:** `engine/distribution/api_publisher.py`
- **Symptom:** Code is written against published Binance OpenAPI specs
  (`/bapi/composite/v1/public/pgc/openApi/content/add` with header
  `X-Square-OpenAPI-Key`) — but the OpenAPI program is **closed to new
  applicants** and we have no key. Implementation is unverified end-to-end.
- **Impact:** `hybrid` mode currently degrades immediately to browser
  mode in practice.
- **Severity:** LOW — browser path is the documented fallback anyway.

### 3.5 Post Tracker — silent dependency on broken reference_feed
- **File:** `engine/analytics/post_tracker.py`
- **Symptom:** Calls `ReferenceFeed.fetch_latest()` internally; when
  that's broken, nothing is recorded, but no loud error.
- **Severity:** MEDIUM — silent-failure mode is the worst kind.

### 3.6 API publisher's `tradingPairs` / `tendency` / `imageList` fields
- **File:** same as 3.4
- **Symptom:** These field names are guesses from the strategy doc; no
  external proof they're correct.
- **Severity:** LOW (until we have a key).

### 3.7 No `bse cookies-import` CLI command actually exists
- **Symptom:** README mentions it; CLI does not. Operator can't easily
  install cookies.
- **Severity:** MEDIUM — quality-of-life blocker.

---

## 4. Missing Critical Components

| # | Component                              | Why we need it                                                  | Effort |
|---|----------------------------------------|-----------------------------------------------------------------|--------|
| 1 | **Cookie ingestion CLI**               | One-shot import + validation of `binance_cookies.json`          | XS     |
| 2 | **Selector tuning tool**               | `bse selectors-tune` opens Square, prints live DOM probes       | M      |
| 3 | **Post-publish verification**          | After publish, re-fetch profile feed; record `external_post_id` | M      |
| 4 | **Burst trigger executor**             | `burst_triggers.yaml` is loaded but never acted on              | M      |
| 5 | **Cooldown per ticker (in-memory)**    | Rate-limiter is DB-based — fine, but no soft "diversity" rule   | S      |
| 6 | **Anti-collision content lock**        | Two slots firing within the jitter window can pick the same opp | S      |
| 7 | **Image preview gate**                 | Before posting, render once + screenshot + show in dashboard    | M      |
| 8 | **Strategy A/B tracker**               | Tag posts with `strategy_variant` so we can split-test          | S      |
| 9 | **Engagement back-fill**               | When PostTracker comes online, walk historical posts            | S      |
| 10| **Production Docker image (multi-stage)** | Current Dockerfile is dev-flavor; need slim runtime          | M      |
| 11| **Supervisord / systemd unit**         | For VPS deployment to survive reboots                           | S      |
| 12| **Log rotation + structured persistence** | Today logs go to `daemon.log` only                           | S      |
| 13| **Health probe + Prometheus metrics**  | For Hostinger / monitoring                                       | M      |
| 14| **Backup + restore scripts**           | SQLite snapshots before each `learn` cycle                      | XS     |
| 15| **Reference-account mining job**       | Schedule a daily scrape of momomomo7171 → write to `data/reference/` | S |

---

## 5. Scalability Problems

| Concern                              | Today                              | At 1,000 posts/month                            |
|--------------------------------------|------------------------------------|-------------------------------------------------|
| SQLite write contention              | OK (single-process)                | Move to Postgres before multi-process daemon    |
| Image disk usage                     | 16 PNG / 0.6 GB after 1 day        | Add nightly purge of >7-day images              |
| Gemini free-tier RPM (5/min)         | OK with fallback chain             | At sustained 40 posts/day this is on the edge   |
| Playwright Chromium memory (~250 MB) | OK on a 1 GB VPS                   | Single-context reuse across publishes (today: relaunches per publish) |
| `refresh_signals` every 5 min        | 12 scans/hr × 80 tickers = 960     | Fine                                            |
| News RSS fetched every 5 min         | Fine                               | Switch to RSS conditional GET (Etag/Last-Modified) |

---

## 6. Security Problems

1. **Cookies on disk in plaintext** — `data/runtime/binance_cookies.json`
   has full session auth. Fix: chmod 600 + warn on commit; add
   `bse cookies-rotate` for periodic re-export.
2. **GEMINI_API_KEY printed in HTTP query string** — logs may contain
   the key in `HTTP Request: ... ?key=…`. Today httpx logger does this
   on httpx info level. Fix: filter `?key=` in our logger.
3. **Dashboard auth is just the tunnel's basic-auth** — fine for
   dev exposure, but on a real VPS we must add `DASHBOARD_AUTH_TOKEN`
   middleware.
4. **No secret-rotation hooks** — env vars are read once at startup.

---

## 7. Deployment Problems

1. `Dockerfile` installs **dev** deps (`pip install -e ".[dev]"`).
   Production image should not include `ruff`, `pytest`, etc.
2. No `HEALTHCHECK` directive in Dockerfile.
3. `docker-compose.yml` has no restart policy other than `unless-stopped`
   (fine) but no `depends_on` for Postgres (we don't have one yet).
4. Playwright Chromium is installed at build time but its system deps
   (libnss3, libasound2, etc.) need explicit apt packages — verified
   missing on slim base images.
5. No `.dockerignore` — `data/`, `logs/`, `.venv/`, `__pycache__/`
   are getting baked in.
6. No persistent volume for SQLite or runtime images in compose.
7. No documented Hostinger / VPS provisioning recipe.

---

## 8. API Problems

1. **OpenAPI key gate** — `apply.binance.com` no longer accepts new
   creator-tier keys as of mid-2025. The `api` publish mode is dead
   for new accounts.
2. **`/bapi/composite/v1/public/pgc/openApi/content/add`** — even with
   a key, the `tradingPairs`, `imageList`, `tendency` fields haven't
   been validated by us.
3. **No CSRF token handling** — most `/bapi/composite/.../friendly/`
   endpoints need `csrftoken` + `bnc-uuid` headers after login.

---

## 9. Publishing Problems

1. Browser publisher does not wait for the **post-publish
   confirmation toast** before considering itself done. Race condition:
   if the network is slow, we close the tab while the request is in
   flight.
2. No screenshot saved on failure for debugging.
3. No DOM diff between attempts — when a selector misses, we don't log
   which alternatives were tried.
4. The "human typing" path uses `locator.type()` with 30-110 ms delay
   per character; for a 70-char post that's 2-7 seconds — long enough
   to be flagged in some anti-bot setups. Need jitter that occasionally
   resembles paste.

---

## 10. Image Upload Problems (THE BIG ONE)

The strategy doc requires that the image appear **inside** the Binance
Square post (not as an ImgBB link in the body).

| Option            | Feasibility                          | Verdict                                  |
|-------------------|--------------------------------------|------------------------------------------|
| A. Hidden OpenAPI endpoint | Inspected 6+ candidates; none accept multipart `image` field without an authenticated session. | DEAD without a real OpenAPI key. |
| B. Browser upload (our path) | Verified Playwright can drive `<input type="file">` on similar SPAs. Works on a logged-in real Square account. | **PRIMARY APPROACH.** Needs cookies. |
| C. Pre-upload to Binance's CDN (`upload-asset` endpoints), then post via API | Inspected; CDN endpoint exists but signs with the user's session keypair. | Possible *after* we can sniff the network from a logged-in session. |
| D. Hybrid: API for text-only posts + browser for image posts | Today's `hybrid` mode | **CHEAP WIN** for text-only burst posts.|

**The plan:** B is the only path that works today with what we have. We
build C as a fallback for when we want to skip the browser entirely
(faster, less anti-bot risk), but we do not block on it.

---

## 11. Risk Analysis

| Risk                         | Likelihood | Impact | Mitigation                                  |
|------------------------------|------------|--------|---------------------------------------------|
| Account ban (TOS violation)  | **HIGH**   | Severe | Cap at 10-15/day for first 2 weeks; jitter; observe |
| Gemini quota exhaustion      | Medium     | Medium | Fallback chain + few-shot fallback (DONE)    |
| Selector drift               | High       | High   | Selector-tuner tool + alerting on consecutive failures |
| Browser detection            | Medium     | Severe | Single persistent profile; human delays; no stealth (per ethical constraint) |
| Reputational (low-quality posts) | Medium | Medium | Hook sanitiser + reference style + human review gate for first N |
| Reputational (over-posting same ticker) | High | Medium | 4h-cooldown DB rule + diversification logic |
| Loss of SQLite DB            | Low        | Medium | Daily snapshot to `data/runtime/snapshots/` |
| Cookies expire mid-day       | High       | High   | Detect login wall on `goto`, send `cookies_invalid` alert |

---

## 12. Recommended Next Steps (ROADMAP)

Below is the ordered execution plan. Each task has a concrete
acceptance criterion. **Bolded** items unlock the next ones.

### Phase B — Critical fixes (this PR + follow-ups)

| # | Task                                       | Acceptance                                                   | Pri |
|---|--------------------------------------------|--------------------------------------------------------------|-----|
| 1 | **`bse cookies-import` CLI**               | `bse cookies-import ./c.json` writes + validates             | P0  |
| 2 | **`bse cookies-export` (Playwright launch + manual login)** | One command opens Chromium, user logs in, cookies persist | P0 |
| 3 | **`bse selectors-tune` interactive probe** | Prints first 3 matching selectors per role for the live DOM  | P0  |
| 4 | **Refactor browser_publisher selectors → YAML config** | `data/selectors/binance_square.yaml` editable without code | P1 |
| 5 | **Post-publish verification**              | After click, poll profile feed; set `external_post_id` or fail | P1 |
| 6 | **Reference Feed fix** (network-record approach) | When cookies present, capture the working request from the live UI | P1 |
| 7 | **Trending Endpoint fix** (same approach as #6) | Same                                                       | P2  |
| 8 | **PostTracker resilience**                 | Loud `tracker_disabled_until_reference_feed_fixed` warning   | P2  |
| 9 | **Burst trigger executor**                 | When ranker score > 8.0 + 1h change > 15%, fire out-of-band  | P1  |
| 10| **`bse run --once` + `--for-minutes N`**   | Time-boxed daemon for testing                                 | P2  |

### Phase C — Strategic polish

| # | Task                                       | Acceptance                                                   | Pri |
|---|--------------------------------------------|--------------------------------------------------------------|-----|
| 11| **Production Dockerfile (multi-stage)**    | Final image < 600 MB; no dev deps                            | P1  |
| 12| **systemd / supervisord units**            | `binance-square-engine.service` template in `deploy/`        | P1  |
| 13| **Hostinger / VPS deployment guide**       | `docs/deploy_hostinger.md` step-by-step                      | P1  |
| 14| **Cookie-expiry detection**                | Detect login wall → page-pause + admin notify                | P2  |
| 15| **Diversification rule**                   | Don't pick same `template_name` 2x in a row                  | P3  |
| 16| **Image preview gate** (dashboard)         | `/posts/preview` page with last 5 cards before publish       | P3  |
| 17| **A/B strategy tag**                       | `Post.strategy_variant` + report breakdown                   | P3  |
| 18| **Persistent browser context reuse**       | One Playwright context for many publishes → -250 MB churn    | P3  |
| 19| **Logs to JSON + rotation**                | structlog JSON output + RotatingFileHandler                  | P3  |
| 20| **Daily SQLite snapshot**                  | `data/runtime/snapshots/YYYYMMDD.db.gz`                      | P3  |
| 21| **Reference-account mining job** (when 3.1 fixed) | Daily scrape → `data/reference/momomomo7171_YYYYMMDD.json`  | P3  |

### Phase D — Optional / future

| # | Task                                       | Notes                                                        |
|---|--------------------------------------------|--------------------------------------------------------------|
| 22| Multi-account support                      | One DB, many cookies/profiles                                |
| 23| Twitter / X re-syndication                 | Reuse hook → tweet from same post                            |
| 24| Telegram channel cross-post                | Per strategy doc                                              |
| 25| Multi-language post variants (en, ar, zh)  | LLM is fluent in all three                                    |
| 26| OpenAI fallback for content                | If Gemini permanently rate-limits                            |

---

## Appendix A — Code-quality observations

**Strengths:**
- Clean module boundaries; layers map 1:1 to strategy doc.
- All public coroutines are typed; pydantic-settings centralizes config.
- Tenacity retry decorators on every network call.
- Structlog usage is consistent; logs are searchable.
- Tests cover sanitiser, rate-limiter, tendency classifier, news
  parser, card renderer (16/16 passing).

**Soft issues to clean up later:**
- `engine/cli.py` is monolithic (294 LOC) — break into sub-commands.
- `engine/visuals/card_renderer.py` is large (333 LOC) and has some
  template-specific branches that belong in the templates.
- A few `Any` types in `engine/signal/reference_feed.py`; tighten.
- `Post.image_paths` and `Post.image_urls` are both JSON columns; their
  invariants aren't enforced (lengths can drift).

**Things that are NOT broken (despite first impressions):**
- The `hybrid` publish mode currently going to browser is **correct**
  behavior — API path fails with `no_openapi_key_configured` and we
  fall back exactly as designed.
- The `success=False` in the live (non-dry-run) `run-slot` earlier was
  the cookie wall — also correct, expected behaviour.

---

## Appendix B — How to validate this diagnosis

Run these commands; expected results in parentheses:

```bash
bse scan                           # (51-80 opps persisted)
bse hook --ticker BTC --trigger PUMP --tendency 1   # (valid Arabic hook)
bse render --ticker BTC --trigger PUMP --change-24h 12.5  # (2 PNGs in data/runtime/images/)
PUBLISH_MODE=dry_run bse run-slot power_hour       # (slot_completed success=True)
python auto_publish.py                              # (scheduler_started slots=20)
pytest -x -q                                        # (16 passed)
```

All of the above are verified green as of this writing.

**Next milestone:** acceptance criteria for the upcoming Phase B PR are
in section 12. The first commit in this branch will implement items
**1, 2, 3, 4, 5** (cookies + selectors tooling + verification) which
together unlock the first real (non-dry-run) post.
