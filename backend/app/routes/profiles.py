"""Extraction-profile listing route.

Exposes the registered profiles so the UI's platform selector is driven by the
backend registry instead of a hardcoded list that drifts. ``"auto"`` is a
UI-only sentinel for auto-detection (no stored platform override).
"""

from fastapi import APIRouter
from pydantic import BaseModel

from app.services.profiles import registry

router = APIRouter(prefix="/api/profiles", tags=["profiles"])


class ProfileOption(BaseModel):
    value: str
    label: str


# Curated display labels. Profiles without an entry fall back to a humanised
# name, so a newly registered profile still appears (just less prettily) until
# a label is added here.
_LABELS: dict[str, str] = {
    "lazy_tree": "Lazy Tree Nav",
    "collapsible_sidebar": "Collapsible Sidebar",
    "docusaurus": "Docusaurus",
    "mkdocs": "MkDocs",
    "gitbook": "GitBook",
    "flare_webhelp": "Flare WebHelp",
    "flare_html5": "Flare HTML5",
    "intercom": "Intercom",
    "freshdesk": "Freshdesk",
    "confluence": "Confluence",
    "salesforce": "Salesforce",
    "warmup_listgroup": "Warm-up + List Group",
    "category_accordion": "Category Accordion",
    "release_notes": "Release Notes",
    "generic": "Generic (sitemap)",
    "llm": "LLM fallback",
}


def _label(name: str) -> str:
    return _LABELS.get(name, name.replace("_", " ").title())


@router.get("", response_model=list[ProfileOption])
async def list_profiles() -> list[ProfileOption]:
    """Return the platform options for the UI: ``auto`` followed by every
    registered profile in registration order."""
    options = [ProfileOption(value="auto", label="Auto-detect")]
    options += [ProfileOption(value=p.name, label=_label(p.name)) for p in registry.PROFILES]
    return options
