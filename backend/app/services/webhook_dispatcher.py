"""Webhook dispatcher — sends signed POST payloads to configured URLs.

Called after extraction runs complete (and per-page change events). Includes:
- HMAC-SHA256 payload signing (X-DocExtractor-Signature header)
- Retry with exponential backoff (3 attempts: 0s, 5s, 15s)
- Delivery logging to webhook_deliveries for auditing
- Best-effort: never raises to the caller (extraction must not fail due to a
  webhook delivery problem)

Event types:
- new_page          — a page was scraped for the first time
- updated_page      — a page's content changed since the last run
- removed_page      — a page dropped out of the rebuilt TOC
- extraction_complete — the full extraction run finished (summary payload)
"""

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.webhook import WebhookConfig, WebhookDelivery, WebhookEventType

logger = logging.getLogger(__name__)

# Retry schedule: initial POST + 2 retries = 3 total attempts.
RETRY_DELAYS = [0, 5, 15]  # seconds
HTTP_TIMEOUT = httpx.Timeout(15.0, connect=10.0)
SIGNATURE_HEADER = "X-DocExtractor-Signature"
SIGNATURE_PREFIX = "sha256="


def _sign_payload(payload_body: bytes, secret: str) -> str:
    """Return the HMAC-SHA256 signature string for the payload body."""
    digest = hmac.new(
        secret.encode("utf-8"), payload_body, hashlib.sha256,
    ).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def _events_list(webhook: WebhookConfig) -> set[str]:
    """Parse the comma-separated events column into a set."""
    return {e.strip() for e in webhook.events.split(",") if e.strip()}


def _build_payload(
    event_type: str,
    run_id: uuid.UUID | None,
    source_id: uuid.UUID | None,
    source_name: str | None,
    vendor_name: str | None,
    product_name: str | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Construct the JSON payload sent to webhook URLs."""
    payload: dict[str, Any] = {
        "event": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": str(run_id) if run_id else None,
        "source_id": str(source_id) if source_id else None,
        "source_name": source_name,
        "vendor_name": vendor_name,
        "product_name": product_name,
    }
    if extra:
        payload.update(extra)
    return payload


async def _fetch_source_names(
    db: AsyncSession, source_id: uuid.UUID | None,
) -> tuple[str | None, str | None, str | None]:
    """Resolve (source_name, vendor_name, product_name) for a source_id."""
    if source_id is None:
        return None, None, None
    from app.models.source import DocumentationSource
    from app.models.product import Product
    from app.models.vendor import Vendor

    row = (
        await db.execute(
            select(
                DocumentationSource.name.label("source_name"),
                Product.name.label("product_name"),
                Vendor.name.label("vendor_name"),
            )
            .join(DocumentationSource, DocumentationSource.product_id == Product.id)
            .join(Vendor, Product.vendor_id == Vendor.id)
            .where(DocumentationSource.id == source_id)
        )
    ).first()
    if row is None:
        return None, None, None
    return row.source_name, row.vendor_name, row.product_name


async def get_eligible_webhooks(
    db: AsyncSession, source_id: uuid.UUID | None, event_type: str,
) -> list[WebhookConfig]:
    """Return active webhooks subscribed to *event_type* for *source_id*.

    A webhook matches when:
    - is_active is True
    - event_type is in its events list
    - its source_id is NULL (global) OR equals the given source_id

    When *source_id* is None (a global/non-source-scoped event), only global
    webhooks (source_id IS NULL) are returned — source-scoped webhooks are
    skipped because the event isn't for their source.
    """
    query = select(WebhookConfig).where(WebhookConfig.is_active.is_(True))
    if source_id is not None:
        query = query.where(
            (WebhookConfig.source_id.is_(None))
            | (WebhookConfig.source_id == source_id)
        )
    else:
        query = query.where(WebhookConfig.source_id.is_(None))
    webhooks = (await db.execute(query)).scalars().all()
    return [w for w in webhooks if event_type in _events_list(w)]


async def _deliver_one(
    webhook: WebhookConfig,
    payload: dict[str, Any],
    event_type: str,
    run_id: uuid.UUID | None,
    source_id: uuid.UUID | None,
    session_factory,
) -> WebhookDelivery:
    """Deliver a single webhook with retries. Returns the final delivery record.

    Uses its own DB session (via *session_factory*) so delivery persistence never
    interferes with the caller's transaction.
    """
    payload_json = json.dumps(payload, default=str, separators=(",", ":"))
    payload_bytes = payload_json.encode("utf-8")

    headers = {"Content-Type": "application/json"}
    if webhook.secret:
        headers[SIGNATURE_HEADER] = _sign_payload(payload_bytes, webhook.secret)

    delivery = WebhookDelivery(
        webhook_id=webhook.id,
        event_type=event_type,
        run_id=run_id,
        source_id=source_id,
        payload=payload_json,
        attempt=1,
        success=False,
    )

    last_error: str | None = None
    last_status: int | None = None
    response_body: str | None = None

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        for idx, delay in enumerate(RETRY_DELAYS):
            if delay > 0:
                await asyncio.sleep(delay)
            delivery.attempt = idx + 1
            try:
                resp = await client.post(webhook.url, content=payload_bytes, headers=headers)
                last_status = resp.status_code
                response_body = resp.text[:4096] if resp.text else None
                if 200 <= resp.status_code < 300:
                    delivery.success = True
                    delivery.status_code = last_status
                    delivery.response_body = response_body
                    last_error = None
                    break
                last_error = f"HTTP {resp.status_code}: {resp.reason_phrase}"
            except Exception as exc:
                last_error = str(exc)[:2048]
                last_status = None

    if not delivery.success:
        delivery.status_code = last_status
        delivery.error = last_error
        delivery.response_body = response_body

    # Persist the delivery and update webhook stats on a separate session.
    # Best-effort: if persistence fails (e.g. DB unreachable), the delivery
    # result is still returned to the caller — webhook delivery must not be
    # blocked by logging failures.
    try:
        async with session_factory() as db:
            db.add(delivery)
            await db.flush()
            # Update aggregate stats on the webhook row.
            stats_update: dict[str, Any] = {
                "last_status_code": last_status,
                "last_attempt_at": datetime.now(timezone.utc),
                "last_error": last_error,
                "total_deliveries": WebhookConfig.total_deliveries + 1,
            }
            if not delivery.success:
                stats_update["total_failures"] = WebhookConfig.total_failures + 1
            await db.execute(
                update(WebhookConfig).where(WebhookConfig.id == webhook.id).values(**stats_update)
            )
            await db.commit()
    except Exception as exc:
        logger.warning("Failed to persist webhook delivery: %s", exc)

    return delivery


async def dispatch_event(
    db: AsyncSession | None,
    event_type: str,
    run_id: uuid.UUID | None = None,
    source_id: uuid.UUID | None = None,
    extra: dict[str, Any] | None = None,
    session_factory=None,
) -> int:
    """Fire *event_type* to all matching webhooks. Returns the number of deliveries
    attempted. Never raises — webhook failures are logged, not propagated.

    When *db* is None (fire-and-forget mode), a fresh session is created via
    *session_factory* to query eligible webhooks and resolve source names.
    """
    from app.core.database import async_session as default_session_factory
    sf = session_factory or default_session_factory

    try:
        if db is None:
            async with sf() as own_db:
                webhooks = await get_eligible_webhooks(own_db, source_id, event_type)
                if not webhooks:
                    return 0
                source_name, vendor_name, product_name = await _fetch_source_names(own_db, source_id)
        else:
            webhooks = await get_eligible_webhooks(db, source_id, event_type)
            if not webhooks:
                return 0
            source_name, vendor_name, product_name = await _fetch_source_names(db, source_id)

        # Dispatch all webhooks concurrently — they're independent.
        tasks = [
            _deliver_one(
                webhook=w,
                payload=_build_payload(
                    event_type=event_type,
                    run_id=run_id,
                    source_id=source_id,
                    source_name=source_name,
                    vendor_name=vendor_name,
                    product_name=product_name,
                    extra=extra,
                ),
                event_type=event_type,
                run_id=run_id,
                source_id=source_id,
                session_factory=sf,
            )
            for w in webhooks
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        delivered = sum(1 for r in results if not isinstance(r, Exception))
        for r in results:
            if isinstance(r, Exception):
                logger.warning("Webhook delivery error: %s", r)
        return delivered
    except Exception as exc:
        logger.warning("dispatch_event failed for %s: %s", event_type, exc)
        return 0


async def send_test_ping(webhook: WebhookConfig, session_factory=None) -> WebhookDelivery:
    """Send a test ping to a webhook URL. Used by the POST /{id}/test route."""
    from app.core.database import async_session as default_session_factory
    sf = session_factory or default_session_factory

    payload = _build_payload(
        event_type="test",
        run_id=None,
        source_id=webhook.source_id,
        source_name=None,
        vendor_name=None,
        product_name=None,
        extra={"message": "DocExtractor webhook test ping"},
    )
    return await _deliver_one(
        webhook=webhook,
        payload=payload,
        event_type="test",
        run_id=None,
        source_id=webhook.source_id,
        session_factory=sf,
    )