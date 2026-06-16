"""DocExtractor — FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.database import engine, Base

# Import all models so Base.metadata is populated before create_all runs.
# Without these imports, Base.metadata is empty and create_all is a no-op.
import app.models  # noqa: F401

from app.routes import (
    vendors_router,
    sources_router,
    extraction_router,
    articles_router,
    export_router,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    # Create tables on startup (dev convenience; production uses Alembic)
    # Models are imported above so Base.metadata is populated.
    logger.info("Creating database tables if they don't exist...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables ready.")
    yield
    await engine.dispose()


app = FastAPI(
    title="DocExtractor",
    description="Extract complete product documentation from vendor URLs, store in PostgreSQL, export as Markdown.",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — allow frontend dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routes
app.include_router(vendors_router)
app.include_router(sources_router)
app.include_router(extraction_router)
app.include_router(articles_router)
app.include_router(export_router)


@app.get("/api/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "version": "0.1.0"}