"""Unit tests for coin priority engine — uses synthetic opportunities."""

import os
import tempfile

import pytest

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp.name}"
os.environ["GEMINI_API_KEY"] = ""
os.environ["LLM_PROVIDER"] = "mock"

import engine.config as cfg  # noqa: E402
import engine.db as db  # noqa: E402

cfg._settings = None
db._engine = None
db._session_factory = None

from engine.db import init_db, session_scope  # noqa: E402
from engine.growth.coin_priority_engine import score_one  # noqa: E402
from engine.models import Opportunity  # noqa: E402


@pytest.fixture(autouse=True)
async def fresh_db():
    await init_db()
    yield


@pytest.mark.asyncio
async def test_score_basic_volume_signal():
    o = Opportunity(
        ticker="BTC", trigger="VOLUME", change_1h_pct=1.5,
        change_24h_pct=4.0, volume_ratio=8.0, priority_score=5.0,
    )
    async with session_scope() as s:
        s.add(o)
        await s.flush()
        oid = o.id
    async with session_scope() as s:
        opp = await s.get(Opportunity, oid)
        sc = await score_one(opp)
    assert sc.volume_component > 0
    assert sc.composite > 0


@pytest.mark.asyncio
async def test_score_binance_trend_boost():
    a = Opportunity(
        ticker="ETH", trigger="PUMP", change_1h_pct=1.0,
        volume_ratio=2.0, binance_trend_hashtag=None, priority_score=5.0,
    )
    b = Opportunity(
        ticker="ETH", trigger="PUMP", change_1h_pct=1.0,
        volume_ratio=2.0, binance_trend_hashtag="#TopGainers", priority_score=5.0,
    )
    async with session_scope() as s:
        s.add_all([a, b])
        await s.flush()
        aid, bid = a.id, b.id
    async with session_scope() as s:
        sa = await score_one(await s.get(Opportunity, aid))
        sb = await score_one(await s.get(Opportunity, bid))
    assert sb.composite > sa.composite
