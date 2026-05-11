"""FastAPI dashboard for live monitoring + control."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import desc, select

from engine.analytics.reports import build_report
from engine.config import get_settings
from engine.db import init_db, session_scope
from engine.logging_setup import setup_logging
from engine.models import Opportunity, Post, PublishLock

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
        "ts": datetime.now(tz=timezone.utc).isoformat(),
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


@app.post("/api/pause")
async def api_pause(hours: float = 1.0, reason: str = "manual") -> dict:
    if hours <= 0 or hours > 24 * 7:
        raise HTTPException(400, "hours must be 0 < h <= 168")
    async with session_scope() as s:
        s.add(PublishLock(
            paused_until=datetime.now(tz=timezone.utc) + timedelta(hours=hours),
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
<p>API endpoints: <a href="/docs">/docs</a> · <a href="/api/posts/recent">/api/posts/recent</a> ·
   <a href="/api/opportunities">/api/opportunities</a> · <a href="/api/report?hours=24">/api/report</a></p>
<h2>Recent posts</h2><div id="posts">loading…</div>
<h2>Open opportunities</h2><div id="opps">loading…</div>
<script>
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
loadPosts(); loadOpps(); setInterval(()=>{loadPosts(); loadOpps();}, 30000);
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
