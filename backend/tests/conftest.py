"""Suite-wide settings, applied before any test module imports ``app``.

Environment variables override ``backend/.env``, so a local run gets the same
isolation as CI: the developer's .env points at the live Firecrawl, and a test
that leaked a request would otherwise reach the shared scraping stack.
"""
import os

os.environ["DOCEXTRACTOR_FIRECRAWL_API_URL"] = "http://firecrawl.invalid"
# Fail over at once if a test reaches the real docling convert path, instead of
# waiting out the default 120s transient window per call.
os.environ.setdefault("DOCEXTRACTOR_DOCLING_SERVE_TRANSIENT_WINDOW", "0")
# bcrypt's minimum work factor; production keeps the default of 12.
os.environ.setdefault("DOCEXTRACTOR_AUTH_BCRYPT_ROUNDS", "4")
