"""Self-optimisation orchestrator — runs every 24h to close the learning loop.

Sequence (each step is independent; failure in one does NOT abort the others):

    1. Rebuild every `GrowthScoreSnapshot` from the latest engagement data.
    2. Refresh `HookPerformance` and emit `adaptive_hooks.yaml`.
    3. Refresh `CoinPerformance`.
    4. Refresh `ImagePerformance` and emit `adaptive_images.yaml`.
    5. Refresh `PostingWindowPerformance` and emit `adaptive_schedule.yaml`.
    6. Extract `reference_patterns.yaml` from the cached reference posts.

A summary `optimisation_report.yaml` is written to `data/runtime/` so the
dashboard can render the latest cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from engine.config import get_settings
from engine.growth import (
    adaptive_scheduler,
    coin_priority_engine,
    growth_scorer,
    hook_intelligence,
    image_strategy_engine,
    reference_mimicry,
)
from engine.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class OptimisationReport:
    started_at: str
    finished_at: str
    steps: list[dict[str, Any]] = field(default_factory=list)

    def add(self, name: str, ok: bool, info: dict | None = None, error: str | None = None) -> None:
        self.steps.append({
            "name": name, "ok": ok, "info": info or {}, "error": error,
        })


async def run_cycle() -> OptimisationReport:
    started = datetime.now(tz=UTC)
    report = OptimisationReport(
        started_at=started.isoformat(timespec="seconds"),
        finished_at="",
    )

    async def _run(name: str, coro_fn) -> None:
        try:
            result = await coro_fn()
            if isinstance(result, dict):
                info = result
            elif isinstance(result, int):
                info = {"count": result}
            else:
                info = {"result": str(result) if result is not None else None}
            report.add(name, True, info)
            log.info("self_opt_step_ok", step=name, info=info)
        except Exception as e:  # noqa: BLE001
            report.add(name, False, error=str(e))
            log.exception("self_opt_step_failed", step=name, error=str(e))

    await _run("growth_scorer.rebuild_all",       growth_scorer.rebuild_all)
    await _run("hook_intelligence.update",        hook_intelligence.update_from_recent_posts)
    await _run("coin_priority_engine.update",     coin_priority_engine.update_from_posts)
    await _run("image_strategy_engine.update",    image_strategy_engine.update_from_posts)
    await _run("adaptive_scheduler.update",       adaptive_scheduler.update_from_posts)
    await _run("reference_mimicry.extract",       reference_mimicry.extract)

    report.finished_at = datetime.now(tz=UTC).isoformat(timespec="seconds")
    _write_report(report)
    return report


def _write_report(report: OptimisationReport) -> Path:
    settings = get_settings()
    path = settings.runtime_dir / "optimisation_report.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "started_at": report.started_at,
                "finished_at": report.finished_at,
                "steps": report.steps,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def latest_report() -> dict | None:
    settings = get_settings()
    path = settings.runtime_dir / "optimisation_report.yaml"
    if not path.exists():
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("optimisation_report_load_failed", error=str(e))
        return None
