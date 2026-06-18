"""Database engine and session management."""

from sqlalchemy import create_engine as _create_sync_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session as _SyncSession, sessionmaker as _sync_sessionmaker

from app.core.config import settings

engine = create_async_engine(settings.database_url, echo=False, pool_size=20, max_overflow=30)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

sync_engine = _create_sync_engine(settings.database_url_sync, pool_pre_ping=True)
sync_session = _sync_sessionmaker(sync_engine, class_=_SyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    """Base class for all ORM models."""
    pass


async def get_db() -> AsyncSession:
    """FastAPI dependency: yields an async database session."""
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()
