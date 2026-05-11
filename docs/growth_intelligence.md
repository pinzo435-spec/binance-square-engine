# Growth Intelligence Layer — Design & Operation

> Phase D of `binance-square-engine`. Promotes the engine from
> "auto-publisher" to "AI Growth Operator" with explicit Top-10 objective.

## 1. Why this layer exists

The engine before Phase D could **execute** a publishing strategy, but it
could not **learn** from outcomes. Every parameter (hook style, template
pick, posting time, ticker preference) was hand-coded and static.

This layer adds:

- A **single optimisation target** — the per-post `growth_score`.
- **Six adaptive subsystems** that mutate selection probabilities based on
  observed engagement.
- A **self-optimisation loop** that runs every 24 hours.
- A **safety net** that prevents the engine from degenerating into a spam
  bot while it explores.

## 2. Architecture overview

```
┌────────────────────────────────────────────────────────────────────────────┐
│                       publisher.py  ─→  Post stored                        │
│                              │                                             │
│                              ▼                                             │
│                    safety_layer.check()   ⟵ duplicates / cooldown / spam   │
└──────────────────────────────┬─────────────────────────────────────────────┘
                               │  (post lands)
                               ▼
              ┌─────────────────────────────────────┐
              │  post_tracker captures engagement   │
              │  every ~30 min                      │
              └───────┬─────────────────────────────┘
                      │  → EngagementSnapshot row
                      ▼
              ┌─────────────────────────────────────┐
              │  growth_scorer.upsert_for_post()    │
              │  → GrowthScoreSnapshot              │
              └───────┬─────────────────────────────┘
                      │
                      ▼
        ┌──────────────────────────────────────────────────┐
        │  self_optimizer.run_cycle()   [02:00 UTC daily]  │
        │  fans out to:                                    │
        │   ├─ hook_intelligence.update                    │
        │   ├─ coin_priority_engine.update                 │
        │   ├─ image_strategy_engine.update                │
        │   ├─ adaptive_scheduler.update                   │
        │   └─ reference_mimicry.extract                   │
        └────────────────────┬─────────────────────────────┘
                             │  writes adaptive_*.yaml
                             ▼
              read by hook_generator + scheduler.run_slot()
              → next post is shaped by what worked yesterday
```

## 3. The growth score (the optimisation target)

A single composite number per post. Defined in
`engine/growth/growth_scorer.py:WEIGHTS`:

```
growth_score = log1p(views)    * 0.20
             + log1p(likes)    * 1.00
             + log1p(comments) * 1.30
             + log1p(shares)   * 1.60   # the reach multiplier
             + log1p(quotes)   * 1.40
             + velocity_bonus  * 0.40
```

Velocity bonus is `log10(views_per_hour / 50)` clamped to `[0, 3]` —
rewards explosive early traction.

`log1p` damps the heavy tail so that one viral post doesn't crowd out all
learning signal from typical posts.

## 4. Six adaptive subsystems

| Module | Reads | Writes | Decision |
|---|---|---|---|
| `hook_intelligence` | `Post.body_text` | `HookPerformance`, `adaptive_hooks.yaml` | Bias hook category in `hook_generator` |
| `coin_priority_engine` | `Post`, `GrowthScoreSnapshot` | `CoinPerformance` | Boost / penalise `Opportunity` ticker scores |
| `image_strategy_engine` | `Post.template_name`, `GrowthScoreSnapshot` | `ImagePerformance`, `adaptive_images.yaml` | Weighted template pick in `scheduler._do_slot()` |
| `adaptive_scheduler` | `Post.published_at`, `GrowthScoreSnapshot` | `PostingWindowPerformance`, `adaptive_schedule.yaml` | Annotate UTC hours as `promote / neutral / suppress` |
| `reference_mimicry` | `ReferencePost` | `reference_patterns.yaml` | Provide creator cadence/tone target to humans + bots |
| `safety_layer` | `Post` (recent) | (none — pure guard) | Block duplicates, near-dupes, repetitive hooks, cooldown |

All weight calculations are deliberately **bounded** (never < 0.1, never > 2.0)
so a single bad day can't disable a category permanently.

### 4.1 Hook categories

Rule-based classifier (Arabic + English lexicons). Categories:

`curiosity / greed / fear / contrarian / humor / self_deprecation / neutral`

Weights are normalised so the highest-EWMA-growth_score category gets ~2.0
and the lowest gets ~0.4. The hook generator does softmax sampling with
temperature 0.9 (slight exploit bias).

### 4.2 Coin priority composite

```
coin_score = 1.2 * volume_spike(opp.volume_ratio)
           + 0.8 * volatility(|Δ1h|)
           + 2.5 * binance_trend_hashtag_match
           + 1.5 * historical_growth_weight
           - 1.0 * recency_penalty   # if posted < 4h ago and not in top 1%
```

A ticker can override the cooldown only if its live opportunity score is
≥ 9.0 (top ~1% on the existing ranker scale).

### 4.3 Posting windows

Per-hour-of-day (UTC) rolling average growth score. Top-quartile hours are
flagged `promote`, bottom-quartile `suppress`. The static cron schedule is
**not overwritten** — instead the scheduler uses the YAML as a hint for
future burst placement.

### 4.4 Reference mimicry

Extracts patterns from `ReferencePost` rows:

- cadence (posts/day)
- typical burst size (≥ 2 posts within 5 min)
- peak hours (top 3 UTC hours)
- average body length & median emoji density
- ticker diversity ratio
- top tickers used
- tone mix (using the same hook classifier)

Output is informational — the engine doesn't auto-copy, it only nudges.

### 4.5 Safety guards (in order, first hit blocks)

1. **Identical body** in last 168h.
2. **Near-duplicate** (Jaccard ≥ 0.85) in last 24h.
3. **Repetitive category** — same hook category 4 times in a row.
4. **Coin cooldown** — same ticker within 4h, unless opp.score ≥ 9.0.
5. **Hourly quality cap** — 6 successful posts in last 60 min (quality cap
   beyond rate-limiter's hard 4/hour).

## 5. The self-optimisation cycle

```python
# engine/growth/self_optimizer.py
async def run_cycle():
    await growth_scorer.rebuild_all()         # 1
    await hook_intelligence.update()          # 2  + writes adaptive_hooks.yaml
    await coin_priority_engine.update()       # 3
    await image_strategy_engine.update()      # 4  + writes adaptive_images.yaml
    await adaptive_scheduler.update()         # 5  + writes adaptive_schedule.yaml
    await reference_mimicry.extract()         # 6  + writes reference_patterns.yaml
```

Each step is independent — one failure does not abort the cycle. A summary
`optimisation_report.yaml` is written so the dashboard can show progress.

Scheduled via APScheduler at **02:00 UTC** daily, also triggerable on
demand via `bse optimise` or POST `/api/growth/optimise-now`.

## 6. Database schema

| Table | Cardinality | Purpose |
|---|---|---|
| `hook_performance` | ~7 rows (one per category) | Hook EWMA |
| `image_performance` | ~5 rows (one per template) | Template EWMA |
| `coin_performance` | grows over time | Per-ticker history |
| `posting_window_performance` | 24 rows max | Per-hour-of-day |
| `engagement_velocity` | one per (post, age) | Velocity time-series |
| `growth_scores` | one per scored post | Current composite score |

All small (< 1 MB for years of data). No migrations needed at this stage —
`init_db()` creates them on first boot.

## 7. Operator surface

### CLI

```bash
bse optimise           # run the 24h cycle on demand
bse growth-report      # print current hook/coin/window/template weights
```

### Dashboard

```
/api/growth/today           today's posts ranked by growth_score
/api/growth/top-posts       top-N by growth_score (any time)
/api/growth/velocity        last 50 engagement snapshots, per-hour rates
/api/growth/coins           coin_performance leaderboard
/api/growth/hooks           hook category leaderboard + current weights
/api/growth/images          template leaderboard + current weights
/api/growth/windows         per-hour-of-day leaderboard + weights
/api/growth/reference       extracted reference-account patterns
/api/growth/optimisation    last optimisation_report.yaml
/api/growth/optimise-now    [POST] trigger cycle now
/api/system/health          quick operator check
```

The dashboard index page shows:

- 5 KPI tiles (publish_mode, open opps, last post age, today's posts, last self-opt)
- Today's top-5 posts by growth_score
- Hook category leaderboard
- Coin leaderboard
- Recent posts (existing)
- Open opportunities (existing)

## 8. Safe defaults

- **First 5 samples** in any category go in unweighted (warm-up).
- **No category is ever fully disabled** (min weight 0.4 for hooks, 0.3 for
  templates, 0.2 for windows).
- **The static daily schedule is not overwritten** — adaptive_scheduler
  only provides hints.
- **The hard rate limiter still runs first** — safety_layer is additional
  quality guard.

## 9. What this layer does NOT do (yet)

- Click-through tracking (we estimate CTR statically at 1.5%).
- Follower-growth attribution per post.
- Cross-account pattern aggregation.
- Re-prompting Gemini with concrete few-shots from the last 7 days
  (`prompt_updater.py` is wired separately but doesn't yet consume the
  growth scores — wiring is a 30-min follow-up).
- Automatic strategy rotation per Top-10 ranking position.

## 10. Failure modes & mitigations

| Failure | Mitigation |
|---|---|
| Zero engagement data on day 1 | Warm-up: weights stay at 1.0 until 5 samples |
| Single viral post skews everything | log1p damps; EWMA bounded to [0.4, 2.0] |
| Reference feed unreachable | extract() returns None; rest of cycle continues |
| Adaptive YAML file corrupted | `load_*` falls back to uniform weights |
| Safety layer false-positive on a unique post | Operator can disable via env `SAFETY_DISABLED=1` (TODO) |
