"""FastAPI dashboard for live monitoring + control."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import desc, select

from engine.analytics.reports import build_report
from engine.config import get_settings
from engine.db import init_db, session_scope
from engine.growth import (
    adaptive_scheduler as gi_scheduler,
)
from engine.growth import (
    hook_intelligence as gi_hooks,
)
from engine.growth import (
    image_strategy_engine as gi_images,
)
from engine.growth import (
    reference_mimicry as gi_ref,
)
from engine.growth import (
    self_optimizer as gi_opt,
)
from engine.logging_setup import setup_logging
from engine.models import (
    CoinPerformance,
    EngagementSnapshot,
    GrowthScoreSnapshot,
    HookPerformance,
    ImagePerformance,
    Opportunity,
    Post,
    PostingWindowPerformance,
    PublishLock,
)

setup_logging()
settings = get_settings()

app = FastAPI(
    title="binance-square-engine dashboard",
    version="0.1.0",
    docs_url="/docs",
    redoc_url=None,
)


@app.on_event("startup")
async def _startup() -> None:
    await init_db()


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "ts": datetime.now(tz=UTC).isoformat(),
        "publish_mode": settings.publish_mode,
        "max_per_day": settings.max_posts_per_day,
    }


@app.get("/api/posts/recent")
async def api_recent(limit: int = Query(20, le=200)) -> list[dict]:
    async with session_scope() as s:
        rows = (await s.execute(
            select(Post).order_by(desc(Post.created_at)).limit(limit)
        )).scalars().all()
    return [
        {
            "id": r.id,
            "ticker": r.ticker,
            "body": r.body_text,
            "tendency": r.tendency,
            "trading_pairs": r.trading_pairs,
            "image_urls": r.image_urls,
            "template": r.template_name,
            "status": r.status,
            "external_id": r.external_post_id,
            "publish_mode": r.publish_mode,
            "published_at": r.published_at.isoformat() if r.published_at else None,
            "created_at": r.created_at.isoformat(),
            "error": r.error,
        }
        for r in rows
    ]


@app.get("/api/opportunities")
async def api_opps(limit: int = Query(20, le=200), only_open: bool = True) -> list[dict]:
    async with session_scope() as s:
        q = select(Opportunity).order_by(desc(Opportunity.priority_score)).limit(limit)
        if only_open:
            q = q.where(Opportunity.consumed.is_(False))
        rows = (await s.execute(q)).scalars().all()
    return [
        {
            "id": r.id,
            "ticker": r.ticker,
            "trigger": r.trigger,
            "score": r.priority_score,
            "change_1h": r.change_1h_pct,
            "change_24h": r.change_24h_pct,
            "template": r.suggested_template,
            "tendency": r.suggested_tendency,
            "trend_hashtag": r.binance_trend_hashtag,
            "discovered_at": r.discovered_at.isoformat(),
            "consumed": r.consumed,
        }
        for r in rows
    ]


@app.get("/api/report")
async def api_report(hours: int = 24) -> dict:
    rep = await build_report(hours)
    return {
        "period_hours": rep.period_hours,
        "total_posts": rep.total_posts,
        "avg_views": rep.avg_views,
        "avg_likes": rep.avg_likes,
        "avg_comments": rep.avg_comments,
        "avg_engagement": rep.avg_engagement,
        "by_template": [r.__dict__ for r in rep.by_template],
        "by_ticker": [r.__dict__ for r in rep.by_ticker],
        "by_hour": [r.__dict__ for r in rep.by_hour],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Growth Intelligence endpoints (Phase D)
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/api/growth/today")
async def api_growth_today() -> dict:
    """Today's performance: posts, growth_score, top mover."""
    start = datetime.now(tz=UTC).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    async with session_scope() as s:
        posts = (await s.execute(
            select(Post)
            .where(Post.created_at >= start)
            .order_by(desc(Post.created_at))
        )).scalars().all()
        scores = (await s.execute(select(GrowthScoreSnapshot))).scalars().all()
    score_by_post = {sc.post_id: sc.growth_score for sc in scores}
    enriched = sorted(
        ({"id": p.id, "ticker": p.ticker, "body": p.body_text,
          "template": p.template_name, "status": p.status,
          "growth_score": round(score_by_post.get(p.id, 0.0), 3)}
         for p in posts),
        key=lambda r: r["growth_score"],
        reverse=True,
    )
    return {
        "date_utc": start.date().isoformat(),
        "posts_count": len(posts),
        "avg_growth_score": round(
            sum(score_by_post.get(p.id, 0.0) for p in posts) / max(1, len(posts)), 3
        ),
        "top_5": enriched[:5],
    }


@app.get("/api/growth/top-posts")
async def api_growth_top_posts(limit: int = Query(20, le=200)) -> list[dict]:
    async with session_scope() as s:
        rows = (await s.execute(
            select(Post, GrowthScoreSnapshot)
            .join(GrowthScoreSnapshot, GrowthScoreSnapshot.post_id == Post.id)
            .order_by(desc(GrowthScoreSnapshot.growth_score))
            .limit(limit)
        )).all()
    return [
        {
            "id": p.id, "ticker": p.ticker, "body": p.body_text,
            "template": p.template_name,
            "views": g.last_view_count, "likes": g.last_like_count,
            "comments": g.last_comment_count, "shares": g.last_share_count,
            "growth_score": round(g.growth_score, 3),
            "velocity_bonus": round(g.velocity_bonus, 3),
            "age_hours": round(g.age_hours, 2),
            "published_at": p.published_at.isoformat() if p.published_at else None,
        }
        for p, g in rows
    ]


@app.get("/api/growth/velocity")
async def api_growth_velocity() -> list[dict]:
    """Last 50 engagement snapshots collapsed to per-hour rates."""
    async with session_scope() as s:
        rows = (await s.execute(
            select(EngagementSnapshot)
            .order_by(desc(EngagementSnapshot.captured_at))
            .limit(50)
        )).scalars().all()
    out = []
    for r in rows:
        age = max(r.age_hours, 0.25)
        out.append({
            "post_id": r.post_id,
            "captured_at": r.captured_at.isoformat(),
            "age_hours": round(r.age_hours, 2),
            "views_per_hour": round(r.view_count / age, 2),
            "likes_per_hour": round(r.like_count / age, 2),
            "comments_per_hour": round(r.comment_count / age, 2),
            "engagement_score": round(r.engagement_score, 3),
        })
    return out


@app.get("/api/growth/coins")
async def api_growth_coins(limit: int = Query(30, le=200)) -> list[dict]:
    async with session_scope() as s:
        rows = (await s.execute(
            select(CoinPerformance)
            .order_by(desc(CoinPerformance.avg_growth_score))
            .limit(limit)
        )).scalars().all()
    return [
        {
            "ticker": r.ticker, "samples": r.samples,
            "avg_growth_score": round(r.avg_growth_score, 3),
            "avg_views": round(r.avg_views, 0),
            "historical_weight": round(r.historical_weight, 3),
            "last_posted_at": r.last_posted_at.isoformat() if r.last_posted_at else None,
        }
        for r in rows
    ]


@app.get("/api/growth/hooks")
async def api_growth_hooks() -> dict:
    async with session_scope() as s:
        rows = (await s.execute(
            select(HookPerformance).order_by(desc(HookPerformance.avg_growth_score))
        )).scalars().all()
    return {
        "weights_file": gi_hooks.load_weights(),
        "categories": [
            {
                "category": r.category, "samples": r.samples,
                "avg_views": round(r.avg_views, 0), "avg_likes": round(r.avg_likes, 1),
                "avg_engagement": round(r.avg_engagement, 3),
                "avg_growth_score": round(r.avg_growth_score, 3),
                "weight": round(r.weight, 3),
            }
            for r in rows
        ],
    }


@app.get("/api/growth/images")
async def api_growth_images() -> dict:
    async with session_scope() as s:
        rows = (await s.execute(
            select(ImagePerformance).order_by(desc(ImagePerformance.avg_growth_score))
        )).scalars().all()
    return {
        "weights_file": gi_images.load_weights(),
        "templates": [
            {
                "template_name": r.template_name, "samples": r.samples,
                "avg_views": round(r.avg_views, 0),
                "avg_engagement": round(r.avg_engagement, 3),
                "avg_growth_score": round(r.avg_growth_score, 3),
                "weight": round(r.weight, 3),
            }
            for r in rows
        ],
    }


@app.get("/api/growth/windows")
async def api_growth_windows() -> dict:
    async with session_scope() as s:
        rows = (await s.execute(
            select(PostingWindowPerformance).order_by(PostingWindowPerformance.hour_utc)
        )).scalars().all()
    return {
        "weights_file": gi_scheduler.load_weights(),
        "hours": [
            {
                "hour_utc": r.hour_utc, "samples": r.samples,
                "avg_views": round(r.avg_views, 0),
                "avg_growth_score": round(r.avg_growth_score, 3),
                "weight": round(r.weight, 3),
            }
            for r in rows
        ],
    }


@app.get("/api/growth/reference")
async def api_growth_reference() -> dict:
    pats = gi_ref.load_patterns()
    return {"patterns": pats}


@app.get("/api/growth/optimisation")
async def api_growth_opt() -> dict:
    return {"latest_report": gi_opt.latest_report()}


@app.post("/api/growth/optimise-now")
async def api_growth_opt_now() -> dict:
    report = await gi_opt.run_cycle()
    return {
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "steps": report.steps,
    }


@app.get("/api/system/health")
async def api_system_health() -> dict:
    """Quick operator status check."""
    now = datetime.now(tz=UTC).replace(tzinfo=None)
    async with session_scope() as s:
        last_post = (await s.execute(
            select(Post).order_by(desc(Post.created_at)).limit(1)
        )).scalar_one_or_none()
        open_opps = (await s.execute(
            select(Opportunity).where(Opportunity.consumed.is_(False))
        )).scalars().all()
        publish_lock = (await s.execute(
            select(PublishLock).order_by(desc(PublishLock.updated_at)).limit(1)
        )).scalar_one_or_none()
    last_age_minutes = None
    if last_post is not None:
        last_age_minutes = round((now - last_post.created_at).total_seconds() / 60, 1)
    return {
        "ok": True,
        "publish_mode": settings.publish_mode,
        "max_per_day": settings.max_posts_per_day,
        "open_opportunities": len(open_opps),
        "last_post_age_minutes": last_age_minutes,
        "paused_until": (
            publish_lock.paused_until.isoformat()
            if publish_lock and publish_lock.paused_until else None
        ),
        "latest_optimisation_finished_at": (
            (gi_opt.latest_report() or {}).get("finished_at")
        ),
    }


@app.post("/api/pause")
async def api_pause(hours: float = 1.0, reason: str = "manual") -> dict:
    if hours <= 0 or hours > 24 * 7:
        raise HTTPException(400, "hours must be 0 < h <= 168")
    async with session_scope() as s:
        s.add(PublishLock(
            paused_until=datetime.now(tz=UTC) + timedelta(hours=hours),
            reason=reason,
        ))
    return {"paused_for_hours": hours, "reason": reason}


@app.post("/api/resume")
async def api_resume() -> dict:
    async with session_scope() as s:
        s.add(PublishLock(paused_until=None, reason="resumed"))
    return {"status": "resumed"}


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return """\
<!doctype html>
<html><head><meta charset="utf-8"><title>binance-square-engine</title>
<style>
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#0b0e11; color:#eaecef;
         margin:0; padding:32px; }
  h1 { color:#F0B90B; } a { color:#F0B90B; }
  pre, code { background:#1e2329; padding:2px 6px; border-radius:6px; color:#eaecef; }
  table { width:100%; border-collapse:collapse; margin-top:16px; }
  th,td { border-bottom:1px solid #2b3139; padding:8px 10px; text-align:left; font-size:14px; }
  th { color:#848e9c; font-weight:600; }
  .badge { display:inline-block; padding:2px 10px; border-radius:999px; font-size:12px; }
  .ok { background:#0ECB81; color:#0b0e11; } .err { background:#F6465D; color:white; }
  .pending { background:#848e9c; color:#0b0e11; }
</style></head>
<body>
<h1>binance-square-engine</h1>
<p>API: <a href="/docs">/docs</a> · <a href="/api/posts/recent">recent</a> ·
   <a href="/api/opportunities">opportunities</a> · <a href="/api/report?hours=24">report</a> ·
   <a href="/api/growth/today">growth/today</a> ·
   <a href="/api/growth/top-posts">growth/top-posts</a> ·
   <a href="/api/growth/hooks">growth/hooks</a> ·
   <a href="/api/growth/coins">growth/coins</a> ·
   <a href="/api/growth/images">growth/images</a> ·
   <a href="/api/growth/windows">growth/windows</a> ·
   <a href="/api/system/health">system/health</a></p>

<div id="kpi" style="display:flex;gap:18px;flex-wrap:wrap;margin:12px 0 20px;"></div>

<h2>Today's top-5 by growth_score</h2><div id="top5">loading…</div>
<h2>Top hooks by category</h2><div id="hooks">loading…</div>
<h2>Top coins by historical growth</h2><div id="coins">loading…</div>
<h2>Recent posts</h2><div id="posts">loading…</div>
<h2>Open opportunities</h2><div id="opps">loading…</div>

<script>
function tile(label, value, sub){
  return `<div style="background:#1e2329;border:1px solid #2b3139;border-radius:10px;padding:14px 18px;min-width:180px;">
    <div style="color:#848e9c;font-size:12px;">${label}</div>
    <div style="font-size:24px;font-weight:600;color:#F0B90B;">${value}</div>
    <div style="color:#848e9c;font-size:12px;">${sub||''}</div></div>`;
}
async function loadHealth() {
  const [h, today] = await Promise.all([
    fetch('/api/system/health').then(r=>r.json()),
    fetch('/api/growth/today').then(r=>r.json()),
  ]);
  document.getElementById('kpi').innerHTML =
    tile('publish_mode', h.publish_mode, `cap ${h.max_per_day}/day`)
    + tile('open opportunities', h.open_opportunities, '')
    + tile('last post age', (h.last_post_age_minutes ?? '∞') + 'm', '')
    + tile("today's posts", today.posts_count, `avg score ${today.avg_growth_score}`)
    + tile('last self-opt', (h.latest_optimisation_finished_at||'never').slice(0,16).replace('T',' '), '');
  document.getElementById('top5').innerHTML = renderTop5(today.top_5||[]);
}
function renderTop5(rows){
  if(!rows.length) return '<p style="color:#848e9c;">No posts today yet.</p>';
  return `<table><tr><th>ticker</th><th>template</th><th>status</th><th>growth_score</th><th>body</th></tr>${
    rows.map(r => `<tr><td>${r.ticker}</td><td>${r.template||'-'}</td><td>${r.status}</td>
       <td>${r.growth_score}</td><td>${(r.body||'').slice(0,80)}</td></tr>`).join('')
  }</table>`;
}
async function loadHooks() {
  const data = (await fetch('/api/growth/hooks').then(r=>r.json())).categories || [];
  document.getElementById('hooks').innerHTML =
    `<table><tr><th>category</th><th>samples</th><th>avg_views</th><th>avg_growth</th><th>weight</th></tr>${
      data.map(d => `<tr><td>${d.category}</td><td>${d.samples}</td><td>${d.avg_views}</td>
         <td>${d.avg_growth_score}</td><td>${d.weight}</td></tr>`).join('')
    }</table>`;
}
async function loadCoins() {
  const data = await fetch('/api/growth/coins?limit=15').then(r=>r.json());
  document.getElementById('coins').innerHTML =
    `<table><tr><th>ticker</th><th>samples</th><th>avg_views</th><th>avg_growth</th><th>last_posted</th></tr>${
      data.map(d => `<tr><td>${d.ticker}</td><td>${d.samples}</td><td>${d.avg_views}</td>
         <td>${d.avg_growth_score}</td><td>${(d.last_posted_at||'').slice(0,16).replace('T',' ')}</td></tr>`).join('')
    }</table>`;
}
async function loadPosts() {
  const r = await fetch('/api/posts/recent?limit=20'); const data = await r.json();
  const rows = data.map(p => `<tr>
      <td>${(p.published_at||p.created_at||'').slice(0,16).replace('T',' ')}</td>
      <td>${p.ticker}</td>
      <td>${(p.body||'').slice(0,80)}</td>
      <td><span class="badge ${p.status==='published'?'ok':p.status==='failed'?'err':'pending'}">${p.status}</span></td>
      <td>${p.publish_mode}</td>
      <td><code>${p.external_id||'-'}</code></td>
    </tr>`).join('');
  document.getElementById('posts').innerHTML =
    `<table><tr><th>when</th><th>ticker</th><th>body</th><th>status</th><th>mode</th><th>id</th></tr>${rows}</table>`;
}
async function loadOpps() {
  const r = await fetch('/api/opportunities?limit=20'); const data = await r.json();
  const rows = data.map(o => `<tr>
      <td>${o.ticker}</td>
      <td>${o.trigger}</td>
      <td>${o.score.toFixed(1)}</td>
      <td>${(o.change_1h||0).toFixed(2)}%</td>
      <td>${(o.change_24h||0).toFixed(2)}%</td>
      <td>${o.template||''}</td>
      <td>${o.trend_hashtag||'-'}</td>
    </tr>`).join('');
  document.getElementById('opps').innerHTML =
    `<table><tr><th>ticker</th><th>trigger</th><th>score</th><th>Δ1h</th><th>Δ24h</th><th>template</th><th>trend</th></tr>${rows}</table>`;
}
loadHealth(); loadHooks(); loadCoins(); loadPosts(); loadOpps();
setInterval(()=>{loadHealth(); loadHooks(); loadCoins(); loadPosts(); loadOpps();}, 30000);
</script>
</body></html>
"""


def serve() -> None:
    import uvicorn

    uvicorn.run(
        "engine.dashboard:app",
        host=settings.dashboard_host,
        port=settings.dashboard_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    serve()
