"""Scheduler process: periodically reap dead runs and enqueue due schedules.

Run with: python -m app.scheduler  (deploy as a single replica)
"""

import asyncio
import logging

from sqlalchemy import text

# Ensure models are registered before any query runs.
import app.models  # noqa: F401
from app.core.database import async_session
from app.services.scheduling import tick

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger(__name__)

TICK_INTERVAL = 30.0
# Belt-and-suspenders against an accidental second replica.
ADVISORY_LOCK_KEY = 778291


async def run_tick_once() -> None:
    async with async_session() as db:
        locked = (
            await db.execute(
                text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK_KEY}
            )
        ).scalar()
        if not locked:
            logger.info("Another scheduler holds the lock; skipping tick")
            return
        try:
            result = await tick(db)
            if result["enqueued"] or result["reaped"]:
                logger.info("Tick: %s", result)
        finally:
            await db.execute(
                text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK_KEY}
            )
            await db.commit()


async def main_loop() -> None:
    logger.info("Scheduler started (tick=%ss)", TICK_INTERVAL)
    while True:
        try:
            await run_tick_once()
        except Exception:
            logger.exception("Scheduler tick error; will retry next interval")
        await asyncio.sleep(TICK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main_loop())
