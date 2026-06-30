"""Thin async client for the docling-serve REST API (PDF→markdown+structure).

docling-serve runs on the homelab k3s; we consume it over HTTP exactly like
Firecrawl, so no docling/torch dependency is embedded in this image."""
from __future__ import annotations

import asyncio
import base64
import logging
import time

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_PAGE_BREAK = "<!-- docling-page-break -->"
_TERMINAL_OK = "success"
_TERMINAL_FAIL = "failure"

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


_POLL_MAX_TRANSIENT = 5  # consecutive transient GET failures tolerated per call


async def _get_json_with_retry(client, url, headers, deadline) -> dict:
    """GET + parse JSON, tolerating transient HTTP errors (e.g. a 502 from the
    ingress while docling-serve is busy on a large doc) until the deadline. A
    single transient blip must not abandon an in-progress conversion."""
    transient = 0
    while True:
        try:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            return await asyncio.to_thread(r.json)
        except (httpx.HTTPError, ValueError) as exc:
            transient += 1
            if transient > _POLL_MAX_TRANSIENT or time.monotonic() > deadline:
                raise
            logger.warning(
                "docling-serve transient error on %s (%d/%d): %s; retrying",
                url.rsplit("/", 2)[-2:], transient, _POLL_MAX_TRANSIENT, exc,
            )
            await asyncio.sleep(settings.docling_serve_poll_interval)


def _build_options(*, page_range, use_vlm_api, do_ocr, image_export_mode,
                   page_break_placeholder) -> dict:
    options: dict = {
        "to_formats": ["md", "json"],
        "do_ocr": do_ocr,
        "image_export_mode": image_export_mode,
        "table_mode": "accurate",
        "pipeline": "vlm" if use_vlm_api else "standard",
    }
    if page_range is not None:
        options["page_range"] = [page_range[0], page_range[1]]
    if page_break_placeholder:
        options["md_page_break_placeholder"] = page_break_placeholder
    if use_vlm_api:
        options["vlm_pipeline_model_api"] = _vlm_model_api()
    return options


async def convert_async(
    pdf_bytes: bytes,
    *,
    filename: str = "source.pdf",
    page_range: "tuple[int, int] | None" = None,
    use_vlm_api: bool = False,
    do_ocr: bool = False,
    image_export_mode: str = "embedded",
    page_break_placeholder: str = "",
    on_poll=None,
) -> dict:
    """Submit a convert task, poll to completion (calling on_poll each tick),
    then fetch and return the `document` dict. Raises DoclingServeError."""
    body = {
        "sources": [{
            "kind": "file",
            "base64_string": base64.b64encode(pdf_bytes).decode("ascii"),
            "filename": filename,
        }],
        "options": _build_options(
            page_range=page_range, use_vlm_api=use_vlm_api, do_ocr=do_ocr,
            image_export_mode=image_export_mode,
            page_break_placeholder=page_break_placeholder,
        ),
    }
    base = settings.docling_serve_url.rstrip("/")
    headers = {"X-Api-Key": settings.docling_serve_api_key,
               "content-type": "application/json"}
    deadline = time.monotonic() + settings.docling_serve_timeout
    try:
        async with httpx.AsyncClient(timeout=settings.docling_serve_timeout) as client:
            resp = await client.post(base + "/v1/convert/source/async",
                                     headers=headers, json=body)
            resp.raise_for_status()
            status = await asyncio.to_thread(resp.json)
            task_id = status.get("task_id")
            if not task_id:
                raise DoclingServeError("async submit returned no task_id")

            while status.get("task_status") not in (_TERMINAL_OK, _TERMINAL_FAIL):
                if time.monotonic() > deadline:
                    raise DoclingServeError("docling-serve conversion timed out")
                await asyncio.sleep(settings.docling_serve_poll_interval)
                status = await _get_json_with_retry(
                    client, base + f"/v1/status/poll/{task_id}", headers, deadline)
                if on_poll is not None:
                    try:
                        await on_poll(status)
                    except Exception:  # noqa: BLE001 - progress must never crash a run
                        logger.exception("on_poll callback failed")

            if status.get("task_status") == _TERMINAL_FAIL:
                raise DoclingServeError(f"docling-serve task failed: {status}")

            payload = await _get_json_with_retry(
                client, base + f"/v1/result/{task_id}", headers, deadline)
    except (httpx.HTTPError, ValueError) as exc:
        raise DoclingServeError(f"docling-serve async request failed: {exc}") from exc

    if payload.get("status") not in ("success", "partial_success"):
        raise DoclingServeError(
            f"docling-serve status={payload.get('status')!r} errors={payload.get('errors')}"
        )
    doc = payload.get("document")
    if not doc:
        raise DoclingServeError("docling-serve returned no document")
    return doc
