"""Daemon entrypoint. Runs the scheduler forever.

Usage:
    python auto_publish.py

Inside Docker we use this as the CMD.
"""

from __future__ import annotations

import asyncio
import signal

from engine.db import init_db
from engine.distribution.scheduler import EngineScheduler
from engine.logging_setup import get_logger, setup_logging


async def main() -> None:
    setup_logging()
    log = get_logger("auto_publish")

    log.info("starting_engine")
    await init_db()
    sched = EngineScheduler()
    await sched.start_async()

    stop_event = asyncio.Event()

    def _stop(*_):
        log.warning("signal_received_stopping")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    try:
        await stop_event.wait()
    finally:
        sched.scheduler.shutdown(wait=False)
        log.info("engine_stopped")


if __name__ == "__main__":
    asyncio.run(main())
