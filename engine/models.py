"""SQLAlchemy ORM models for the engine's persistent state.

Tables:
- `opportunities`: signals discovered by the Signal Layer awaiting publication.
- `posts`: every post we publish, with content, visuals, and engagement snapshots.
- `engagement_snapshots`: time-series of views/likes/comments collected by the Analytics Layer.
- `templates`: hook templates with running performance metrics.
- `reference_posts`: cached posts from the reference account (momomomo7171) used for learning.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Opportunity(Base):
    __tablename__ = "opportunities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    trigger: Mapped[str] = mapped_column(String(64))  # PUMP / DUMP / VOLUME / LISTING / NEWS / TREND
    change_1h_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_24h_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    binance_trend_hashtag: Mapped[str | None] = mapped_column(String(128), nullable=True)
    priority_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    suggested_template: Mapped[str | None] = mapped_column(String(64), nullable=True)
    suggested_tendency: Mapped[int] = mapped_column(Integer, default=0)  # 0=neutral 1=bull 2=bear
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    posts: Mapped[list["Post"]] = relationship(back_populates="opportunity")


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    opportunity_id: Mapped[int | None] = mapped_column(
        ForeignKey("opportunities.id"), nullable=True
    )
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    body_text: Mapped[str] = mapped_column(Text)
    tendency: Mapped[int] = mapped_column(Integer, default=0)
    trading_pairs: Mapped[list[str]] = mapped_column(JSON, default=list)
    image_paths: Mapped[list[str]] = mapped_column(JSON, default=list)
    image_urls: Mapped[list[str]] = mapped_column(JSON, default=list)
    template_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    publish_mode: Mapped[str] = mapped_column(String(16), default="api")  # api/browser/dry_run
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    external_post_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    opportunity: Mapped["Opportunity"] = relationship(back_populates="posts")
    snapshots: Mapped[list["EngagementSnapshot"]] = relationship(back_populates="post")

    __table_args__ = (Index("ix_posts_ticker_published_at", "ticker", "published_at"),)


class EngagementSnapshot(Base):
    __tablename__ = "engagement_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    age_hours: Mapped[float] = mapped_column(Float, default=0.0)
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    like_count: Mapped[int] = mapped_column(Integer, default=0)
    comment_count: Mapped[int] = mapped_column(Integer, default=0)
    share_count: Mapped[int] = mapped_column(Integer, default=0)
    quote_count: Mapped[int] = mapped_column(Integer, default=0)
    engagement_score: Mapped[float] = mapped_column(Float, default=0.0)

    post: Mapped["Post"] = relationship(back_populates="snapshots")


class Template(Base):
    __tablename__ = "templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    template_text: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(32))  # win/loss/warning/curiosity/etc
    tendency_hint: Mapped[int] = mapped_column(Integer, default=0)
    times_used: Mapped[int] = mapped_column(Integer, default=0)
    avg_views: Mapped[float] = mapped_column(Float, default=0.0)
    avg_likes: Mapped[float] = mapped_column(Float, default=0.0)
    avg_engagement: Mapped[float] = mapped_column(Float, default=0.0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class ReferencePost(Base):
    """Cached posts from the reference account used for few-shot learning."""

    __tablename__ = "reference_posts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # external id
    square_uid: Mapped[str] = mapped_column(String(64), index=True)
    body_text: Mapped[str] = mapped_column(Text)
    tickers: Mapped[list[str]] = mapped_column(JSON, default=list)
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    like_count: Mapped[int] = mapped_column(Integer, default=0)
    comment_count: Mapped[int] = mapped_column(Integer, default=0)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    raw_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    cached_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PublishLock(Base):
    """Single-row table used as a global publishing pause flag."""

    __tablename__ = "publish_lock"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    paused_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
