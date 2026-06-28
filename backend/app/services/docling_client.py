"""Thin async client for the docling-serve REST API (PDF→markdown+structure).

docling-serve runs on the homelab k3s; we consume it over HTTP exactly like
Firecrawl, so no docling/torch dependency is embedded in this image."""
from __future__ import annotations

import base64
import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_VLM_PROMPT = (
    "Convert this page to markdown. Render every table as a proper Markdown "
    "table with correct rows and columns. Do not miss any text and only output "
    "the bare markdown!"
)


class DoclingServeError(Exception):
    """Raised when docling-serve cannot convert a document."""


def _vlm_model_api() -> dict:
    return {
        "url": settings.pdf_vlm_base_url,
        "headers": {"Authorization": f"Bearer {settings.pdf_vlm_api_key}"},
        "params": {"model": settings.pdf_vlm_model},
        "prompt": _VLM_PROMPT,
        # Required by docling-serve's VlmModelApi; we ask the model for markdown.
        "response_format": "markdown",
    }


async def convert(
    pdf_bytes: bytes,
    *,
    filename: str = "source.pdf",
    pipeline: str = "standard",
    page_range: "tuple[int, int] | None" = None,
    use_vlm_api: bool = False,
    do_ocr: bool = False,
    image_export_mode: str = "embedded",
) -> dict:
    """POST a PDF to docling-serve /v1/convert/source; return the `document` dict
    (`md_content`, `json_content`). Raise DoclingServeError on any failure."""
    options: dict = {
        "to_formats": ["md", "json"],
        "do_ocr": do_ocr,
        "image_export_mode": image_export_mode,
        "table_mode": "accurate",
        "pipeline": pipeline,
    }
    if page_range is not None:
        options["page_range"] = [page_range[0], page_range[1]]
    if use_vlm_api:
        options["vlm_pipeline_model_api"] = _vlm_model_api()

    body = {
        "sources": [{
            "kind": "file",
            "base64_string": base64.b64encode(pdf_bytes).decode("ascii"),
            "filename": filename,
        }],
        "options": options,
    }
    url = settings.docling_serve_url.rstrip("/") + "/v1/convert/source"
    headers = {"X-Api-Key": settings.docling_serve_api_key,
               "content-type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=settings.docling_serve_timeout) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise DoclingServeError(f"docling-serve request failed: {exc}") from exc

    if payload.get("status") not in ("success", "partial_success"):
        raise DoclingServeError(
            f"docling-serve status={payload.get('status')!r} "
            f"errors={payload.get('errors')}"
        )
    doc = payload.get("document")
    if not doc:
        raise DoclingServeError("docling-serve returned no document")
    return doc
