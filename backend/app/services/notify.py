"""Best-effort outbound notifications (generic webhook). Never raises.

Used for operator alerts such as a realm session expiring mid-run. POSTs a JSON
payload with ``text``/``content``/``message`` keys so common receivers
(ntfy / Slack / Discord / generic) render something sensible. Disabled when
``settings.notify_webhook_url`` is blank.
"""

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


def _client_factory() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))


async def notify(title: str, message: str, **fields) -> None:
    url = (getattr(settings, "notify_webhook_url", "") or "").strip()
    if not url:
        return
    payload = {
        "title": title,
        "message": message,
        "text": f"{title}: {message}",
        "content": f"{title}: {message}",
        **fields,
    }
    try:
        client = _client_factory()
        try:
            await client.post(url, json=payload)
        finally:
            await client.aclose()
    except Exception as exc:  # best-effort: log and move on, never break the run
        logger.warning("notify webhook failed: %s", exc)
