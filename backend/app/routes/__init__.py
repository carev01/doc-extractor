"""API route modules."""

from app.routes.vendors import router as vendors_router
from app.routes.products import router as products_router
from app.routes.sources import router as sources_router
from app.routes.extraction import router as extraction_router
from app.routes.articles import router as articles_router
from app.routes.export import router as export_router
from app.routes.jobs import router as jobs_router

__all__ = [
    "vendors_router",
    "products_router",
    "sources_router",
    "extraction_router",
    "articles_router",
    "export_router",
    "jobs_router",
]
