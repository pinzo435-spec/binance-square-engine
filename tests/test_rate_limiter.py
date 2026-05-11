import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

# Use a temp DB file for the test
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp.name}"
os.environ["GEMINI_API_KEY"] = ""  # force mock provider
os.environ["LLM_PROVIDER"] = "mock"

# Reset cached settings/engine
import engine.config as cfg
import engine.db as db
cfg._settings = None
db._engine = None
db._session_factory = None

from engine.db import init_db, session_scope  # noqa: E402
from engine.distribution.rate_limiter import RateLimiter  # noqa: E402
from engine.models import Post, PublishLock  # noqa: E402


@pytest.fixture(autouse=True)
async def fresh_db():
    await init_db()
    async with session_scope() as s:
        for row in (await s.execute(
            __import__("sqlalchemy").select(Post)
        )).scalars().all():
            await s.delete(row)
        for row in (await s.execute(
            __import__("sqlalchemy").select(PublishLock)
        )).scalars().all():
            await s.delete(row)
    yield


@pytest.mark.asyncio
async def test_clean_state_allows():
    decision = await RateLimiter().check("BTC")
    assert decision.allowed


@pytest.mark.asyncio
async def test_pause_lock_blocks():
    async with session_scope() as s:
        s.add(PublishLock(
            paused_until=datetime.now(tz=timezone.utc) + timedelta(hours=1),
            reason="test",
        ))
    decision = await RateLimiter().check("BTC")
    assert not decision.allowed
    assert "globally_paused" in decision.reason


@pytest.mark.asyncio
async def test_same_ticker_gap_blocks():
    async with session_scope() as s:
        s.add(Post(
            ticker="BTC", body_text="$BTC test $BTC", tendency=0,
            trading_pairs=["BTCUSDT"], image_paths=[], image_urls=[],
            template_name="t", publish_mode="api",
            published_at=datetime.now(tz=timezone.utc) - timedelta(hours=1),
            status="published",
        ))
    decision = await RateLimiter().check("BTC")
    assert not decision.allowed
    assert "same_ticker_gap" in decision.reason
