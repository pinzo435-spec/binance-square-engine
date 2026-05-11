"""Centralized configuration loader using pydantic-settings.

All env-driven config flows through this module. Components should *never* read
`os.environ` directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime configuration. Values come from environment variables / `.env`."""

    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Identity / branding ----
    account_handle: str = "KinzoTech"
    account_display_name: str = "Kinzo Tech"
    square_uid: str = ""
    reference_square_uid: str = "BGzelAbjfOwj01wOvfmP5g"  # momomomo7171

    # ---- LLM ----
    llm_provider: Literal["gemini", "openai", "mock"] = "gemini"
    llm_model: str = "gemini-1.5-flash"
    gemini_api_key: str = ""
    openai_api_key: str = ""

    # ---- Image hosting ----
    imgbb_api_key: str = ""

    # ---- Publishing ----
    x_square_openapi_key: str = ""
    binance_cookies_path: Path = ROOT_DIR / "data" / "runtime" / "binance_cookies.json"
    publish_mode: Literal["api", "browser", "hybrid", "dry_run"] = "hybrid"

    # ---- Safety rails ----
    max_posts_per_day: int = 40
    max_posts_per_hour: int = 4
    min_gap_same_ticker_hours: int = 4
    pause_if_n_low_views: int = 3
    low_views_threshold: int = 500
    pause_hours: int = 1

    # ---- Database ----
    database_url: str = "sqlite+aiosqlite:///./data/runtime/engine.db"

    # ---- Scheduler ----
    scheduler_timezone: str = "UTC"
    daily_schedule_file: Path = ROOT_DIR / "playbooks" / "daily_schedule.yaml"
    burst_triggers_file: Path = ROOT_DIR / "playbooks" / "burst_triggers.yaml"

    # ---- Logging ----
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"

    # ---- Dashboard ----
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8000
    dashboard_enabled: bool = True

    # ---- News ----
    news_rss_urls: str = Field(
        default="https://cointelegraph.com/rss,https://www.coindesk.com/arc/outboundfeeds/rss"
    )

    # ---- Computed ----
    @property
    def news_rss_list(self) -> list[str]:
        return [u.strip() for u in self.news_rss_urls.split(",") if u.strip()]

    @property
    def root_dir(self) -> Path:
        return ROOT_DIR

    @property
    def runtime_dir(self) -> Path:
        d = ROOT_DIR / "data" / "runtime"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def images_dir(self) -> Path:
        d = self.runtime_dir / "images"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @field_validator("publish_mode", mode="before")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.lower() if isinstance(v, str) else v


_settings: Settings | None = None


def get_settings() -> Settings:
    """Singleton accessor for settings."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
