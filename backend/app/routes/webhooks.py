"""Webhook configuration routes — CRUD, delivery history, and test ping."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.webhook import WebhookConfig, WebhookDelivery
from app.schemas.webhook import (
    WebhookCreate,
    WebhookUpdate,
    WebhookResponse,
    WebhookListResponse,
    WebhookDeliveryResponse,
    WebhookDeliveryListResponse,
    WebhookTestResponse,
)
from app.services.webhook_dispatcher import send_test_ping

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])

_ALL_EVENTS = {"new_page", "updated_page", "removed_page", "extraction_complete"}


def _to_response(w: WebhookConfig) -> WebhookResponse:
    return WebhookResponse(
        id=w.id,
        source_id=w.source_id,
        url=w.url,
        label=w.label,
        events=[e.strip() for e in w.events.split(",") if e.strip()],
        secret=w.secret,
        is_active=w.is_active,
        last_status_code=w.last_status_code,
        last_attempt_at=w.last_attempt_at,
        last_error=w.last_error,
        total_deliveries=w.total_deliveries,
        total_failures=w.total_failures,
        created_at=w.created_at,
        updated_at=w.updated_at,
    )


@router.get("", response_model=WebhookListResponse)
async def list_webhooks(
    source_id: uuid.UUID | None = None,
    is_active: bool | None = None,
    db: AsyncSession = Depends(get_db),
):
    """List all configured webhooks, optionally filtered by source or active state."""
    query = select(WebhookConfig).order_by(WebhookConfig.created_at.desc())
    if source_id is not None:
        # Match both global (NULL) and source-scoped webhooks.
        query = query.where(
            (WebhookConfig.source_id.is_(None)) | (WebhookConfig.source_id == source_id)
        )
    if is_active is not None:
        query = query.where(WebhookConfig.is_active.is_(is_active))
    webhooks = (await db.execute(query)).scalars().all()
    return WebhookListResponse(
        webhooks=[_to_response(w) for w in webhooks],
        total=len(webhooks),
    )


@router.post("", response_model=WebhookResponse, status_code=201)
async def create_webhook(
    data: WebhookCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create a new webhook configuration."""
    events_str = ",".join(data.events) if data.events else "extraction_complete"
    # Validate event types.
    for e in data.events:
        if e not in _ALL_EVENTS:
            raise HTTPException(status_code=422, detail=f"Unknown event type: {e}")
    webhook = WebhookConfig(
        source_id=data.source_id,
        url=str(data.url),
        label=data.label,
        events=events_str,
        secret=data.secret,
        is_active=data.is_active,
    )
    db.add(webhook)
    await db.commit()
    await db.refresh(webhook)
    return _to_response(webhook)


@router.get("/{webhook_id}", response_model=WebhookResponse)
async def get_webhook(webhook_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    webhook = await db.get(WebhookConfig, webhook_id)
    if not webhook:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return _to_response(webhook)


@router.patch("/{webhook_id}", response_model=WebhookResponse)
async def update_webhook(
    webhook_id: uuid.UUID,
    data: WebhookUpdate,
    db: AsyncSession = Depends(get_db),
):
    webhook = await db.get(WebhookConfig, webhook_id)
    if not webhook:
        raise HTTPException(status_code=404, detail="Webhook not found")

    if data.url is not None:
        webhook.url = str(data.url)
    if data.label is not None:
        webhook.label = data.label
    if data.events is not None:
        for e in data.events:
            if e not in _ALL_EVENTS:
                raise HTTPException(status_code=422, detail=f"Unknown event type: {e}")
        webhook.events = ",".join(data.events) if data.events else "extraction_complete"
    if data.secret is not None:
        webhook.secret = data.secret
    if data.is_active is not None:
        webhook.is_active = data.is_active
    if data.source_id is not None:
        webhook.source_id = data.source_id

    await db.commit()
    await db.refresh(webhook)
    return _to_response(webhook)


@router.delete("/{webhook_id}", status_code=204)
async def delete_webhook(webhook_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    webhook = await db.get(WebhookConfig, webhook_id)
    if not webhook:
        raise HTTPException(status_code=404, detail="Webhook not found")
    await db.delete(webhook)
    await db.commit()


@router.post("/{webhook_id}/test", response_model=WebhookTestResponse)
async def test_webhook(webhook_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Send a test ping to the webhook URL to verify connectivity."""
    webhook = await db.get(WebhookConfig, webhook_id)
    if not webhook:
        raise HTTPException(status_code=404, detail="Webhook not found")
    delivery = await send_test_ping(webhook)
    return WebhookTestResponse(
        success=delivery.success,
        status_code=delivery.status_code,
        error=delivery.error,
    )


@router.get("/{webhook_id}/deliveries", response_model=WebhookDeliveryListResponse)
async def list_deliveries(
    webhook_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """List recent delivery attempts for a webhook."""
    webhook = await db.get(WebhookConfig, webhook_id)
    if not webhook:
        raise HTTPException(status_code=404, detail="Webhook not found")
    deliveries = (
        await db.execute(
            select(WebhookDelivery)
            .where(WebhookDelivery.webhook_id == webhook_id)
            .order_by(WebhookDelivery.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return WebhookDeliveryListResponse(
        deliveries=[
            WebhookDeliveryResponse(
                id=d.id,
                webhook_id=d.webhook_id,
                event_type=d.event_type,
                run_id=d.run_id,
                source_id=d.source_id,
                status_code=d.status_code,
                error=d.error,
                attempt=d.attempt,
                success=d.success,
                created_at=d.created_at,
            )
            for d in deliveries
        ],
        total=len(deliveries),
    )