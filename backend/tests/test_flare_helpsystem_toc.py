"""Tests for flare_helpsystem_toc — build a Flare TOC from its Data/ files.

Offline: a FakeScraper serves canned HelpSystem.xml + master TOC + chunk, the
same shapes MadCap Flare HTML5 emits (verified against a live Flare site).
"""

import pytest

from app.services.profiles.scraper import FakeScraper
from app.services.profiles.strategies import flare_helpsystem_toc

ROOT = "https://h.example.com/help/M365/Content/M365_Home.htm"
HELP = "https://h.example.com/help/M365/"

HELPSYSTEM_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<WebHelpSystem DefaultUrl="Content/M365_Home.htm" '
    'Toc="Data/Tocs/OnlineMasterTOC.js" Index="Data/Index.js" />'
)
MASTER_TOC = (
    "define({numchunks:1,prefix:'OnlineMasterTOC_Chunk',"
    "chunkstart:['/Content/a.htm'],"
    "tree:{n:[{i:0,c:0},{i:1,c:0,n:[{i:2,c:0},{i:3,c:0}]}]}})"
)
CHUNK0 = (
    "define({"
    "'/Content/a.htm':{i:[0],t:['Alpha'],b:['']},"
    "'/Content/b.htm':{i:[1],t:['Beta: the second'],b:['']},"
    "'/Content/b1.htm':{i:[2],t:['Beta One'],b:['']},"
    "'/Content/b2.htm':{i:[3],t:['What\\'s next'],b:['']}"
    "})"
)

RAW = {
    HELP + "Data/HelpSystem.xml": HELPSYSTEM_XML,
    HELP + "Data/Tocs/OnlineMasterTOC.js": MASTER_TOC,
    HELP + "Data/Tocs/OnlineMasterTOC_Chunk0.js": CHUNK0,
}


@pytest.mark.asyncio
async def test_builds_hierarchical_toc_from_data_files():
    scraper = FakeScraper({}, raw_by_url=RAW)
    toc = await flare_helpsystem_toc(scraper, ROOT)

    titles = [(e.title, e.level, e.is_article, e.url) for e in toc]
    assert titles == [
        ("Alpha", 0, True, HELP + "Content/a.htm"),
        ("Beta: the second", 0, False, HELP + "Content/b.htm"),  # has children -> section
        ("Beta One", 1, True, HELP + "Content/b1.htm"),
        ("What's next", 1, True, HELP + "Content/b2.htm"),       # JS-escaped apostrophe
    ]
    # children are linked to their parent
    assert toc[2].parent_url == HELP + "Content/b.htm"
    assert toc[3].parent_url == HELP + "Content/b.htm"


@pytest.mark.asyncio
async def test_returns_empty_when_data_files_absent():
    # No raw files served -> get_raw raises -> graceful [] so callers can fall back.
    scraper = FakeScraper({}, raw_by_url={})
    assert await flare_helpsystem_toc(scraper, ROOT) == []
