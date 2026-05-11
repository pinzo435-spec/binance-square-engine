"""Unit tests for safety_layer Jaccard logic + async guard wiring."""

import os
import tempfile
from datetime import UTC, datetime, timedelta

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
from engine.growth.safety_layer import check, jaccard_sync  # noqa: E402
from engine.models import Post  # noqa: E402


@pytest.fixture(autouse=True)
async def fresh_db():
    await init_db()
    async with session_scope() as s:
        for row in (await s.execute(__import__("sqlalchemy").select(Post))).scalars().all():
            await s.delete(row)
    yield


def test_jaccard_identical():
    a = "the quick brown fox jumps over"
    assert jaccard_sync(a, a) == 1.0


def test_jaccard_disjoint():
    a = "the quick brown fox"
    b = "totally different words here entirely"
    assert jaccard_sync(a, b) < 0.2


@pytest.mark.asyncio
async def test_duplicate_body_blocked():
    async with session_scope() as s:
        s.add(Post(
            ticker="BTC", body_text="$BTC انفجار $BTC", status="success",
            created_at=datetime.now(tz=UTC).replace(tzinfo=None),
            published_at=datetime.now(tz=UTC).replace(tzinfo=None),
        ))
    v = await check(body_text="$BTC انفجار $BTC", ticker="BTC")
    assert not v.allow
    assert v.reason == "duplicate_body"


@pytest.mark.asyncio
async def test_clean_post_allowed():
    v = await check(body_text="$ETH شغل جديد على المؤشر $ETH", ticker="ETH")
    assert v.allow


@pytest.mark.asyncio
async def test_coin_cooldown_blocked_unless_override():
    async with session_scope() as s:
        s.add(Post(
            ticker="SOL", body_text="$SOL old post $SOL", status="success",
            created_at=datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(hours=1),
            published_at=datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(hours=1),
        ))
    # Within cooldown, low score → blocked
    v = await check(body_text="$SOL completely new $SOL", ticker="SOL", opportunity_score=5.0)
    assert not v.allow
    assert v.reason == "coin_cooldown"
    # Within cooldown, very high score → allowed (override)
    v2 = await check(body_text="$SOL totally new $SOL", ticker="SOL", opportunity_score=9.5)
    assert v2.allow
