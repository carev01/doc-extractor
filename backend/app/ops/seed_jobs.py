"""One-off: create Tier 1/2/3 and the three staggered, disabled tier jobs.

Jobs run at 02:00 UTC: Tier 1 monthly on the 1st Sunday, Tier 2 every 2 months on
the 1st Saturday, Tier 3 quarterly on the last Saturday, so the tiers rarely fire
together. They are created disabled (``next_run_at`` stays
NULL, so nothing fires); ``cron`` is still derived so the schedule is ready when
the owner enables them. Idempotent; a legacy ``Major competitors`` job is removed
only when it has no targets and no runs.
"""
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job
from app.models.job_run import JobRun
from app.models.job_target import JobTarget
from app.models.tier import Tier
from app.services.cron import job_spec, spec_to_cron, validate_spec

# name -> (sort_order, nth, weekday 0=Sunday, interval_months)
TIERS = {
    "Tier 1": (1, "first", 0, 1),
    "Tier 2": (2, "first", 6, 2),
    "Tier 3": (3, "last", 6, 3),
}


async def seed(db: AsyncSession, now: datetime | None = None) -> dict:
    """Create missing tiers/jobs in the caller's transaction; never commits."""
    next_month = (now or datetime.now(timezone.utc)).month % 12 + 1
    result = {"tiers_created": [], "jobs_created": [], "deleted_major_competitors": False}

    for name, (order, nth, weekday, interval) in TIERS.items():
        tier = (await db.execute(select(Tier).where(Tier.name == name))).scalar_one_or_none()
        if tier is None:
            tier = Tier(name=name, sort_order=order)
            db.add(tier)
            await db.flush()
            result["tiers_created"].append(name)

        job = (await db.execute(select(Job).where(Job.name == name))).scalars().first()
        if job is None:
            job = Job(
                name=name, enabled=False, tier_id=tier.id, schedule_kind="monthly_nth",
                interval_months=interval, start_month=next_month, nth=nth, weekday=weekday,
                time_of_day="02:00", timezone="UTC", next_run_at=None,
            )
            validate_spec(job_spec(job))
            job.cron = spec_to_cron(job_spec(job))
            db.add(job)
            await db.flush()
            result["jobs_created"].append(name)

    legacy = (await db.execute(select(Job).where(Job.name == "Major competitors"))).scalars().first()
    if legacy is not None:
        targets = (await db.execute(
            select(func.count()).select_from(JobTarget).where(JobTarget.job_id == legacy.id))).scalar_one()
        runs = (await db.execute(
            select(func.count()).select_from(JobRun).where(JobRun.job_id == legacy.id))).scalar_one()
        if not targets and not runs:
            await db.delete(legacy)
            await db.flush()
            result["deleted_major_competitors"] = True
    return result


async def run(db_url: str, apply: bool) -> dict:
    """Seed; a dry run (``apply=False``) performs the real writes, then rolls back."""
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    url = make_url(db_url)
    print(f"target: {url.host}:{url.port}/{url.database}  mode: {'APPLY' if apply else 'dry run'}")
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            try:
                result = await seed(db)
            except BaseException:
                await db.rollback()
                raise
            if apply:
                await db.commit()
            else:
                await db.rollback()
            print("result:", result, "COMMITTED" if apply else "ROLLED BACK")
            return result
    finally:
        await engine.dispose()


if __name__ == "__main__":
    import argparse
    import asyncio

    from app.core.config import settings

    ap = argparse.ArgumentParser(description="Seed tiers and tier jobs (one-off).")
    ap.add_argument("--db-url", default=settings.database_url)
    ap.add_argument("--apply", action="store_true", help="commit (default: dry run, rolled back)")
    args = ap.parse_args()
    asyncio.run(run(args.db_url, args.apply))
