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
    "tree:{n:[{i:0,c:0},{i:1,c:0,n:[{i:2,c:0},{i:3,c:0}]},{i:4,c:0}]}})"
)
CHUNK0 = (
    "define({"
    "'/Content/a.htm':{i:[0],t:['Alpha'],b:['']},"
    "'/Content/b.htm':{i:[1],t:['Beta: the second'],b:['']},"
    "'/Content/b1.htm':{i:[2],t:['Beta One'],b:['']},"
    "'/Content/b2.htm':{i:[3],t:['What\\'s next'],b:['']},"
    "'/Content/b3.htm':{i:[4],t:['Datto\\u0027s infra'],b:['']}"
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
        ("What's next", 1, True, HELP + "Content/b2.htm"),       # JS-escaped apostrophe (\')
        ("Datto's infra", 0, True, HELP + "Content/b3.htm"),     # \\uXXXX-escaped apostrophe
    ]
    # children are linked to their parent
    assert toc[2].parent_url == HELP + "Content/b.htm"
    assert toc[3].parent_url == HELP + "Content/b.htm"


@pytest.mark.asyncio
async def test_resolves_root_for_topic_nested_deep_under_content():
    """HTML5 topics can sit several levels below /Content/ (e.g. Datto Continuity
    Content/kb/siris-alto-nas/foo.htm). The help root is the path up to
    "/Content/", not one directory up — regression for finding only 1 page."""
    root = "https://continuity.datto.com/help/Content/kb/siris-alto-nas/applianceLanding.htm"
    help_ = "https://continuity.datto.com/help/"
    raw = {
        help_ + "Data/HelpSystem.xml":
            '<WebHelpSystem Toc="Data/Tocs/OnlineMasterTOC.js" />',
        help_ + "Data/Tocs/OnlineMasterTOC.js":
            "define({numchunks:1,prefix:'OnlineMasterTOC_Chunk',"
            "tree:{n:[{i:0,c:0},{i:1,c:0}]}})",
        help_ + "Data/Tocs/OnlineMasterTOC_Chunk0.js":
            "define({'/Content/kb/siris-alto-nas/applianceLanding.htm':{i:[0],t:['Appliance'],b:['']},"
            "'/Content/kb/siris-alto-nas/setup.htm':{i:[1],t:['Setup'],b:['']}})",
    }
    scraper = FakeScraper({}, raw_by_url=raw)
    toc = await flare_helpsystem_toc(scraper, root)
    assert [(e.title, e.url) for e in toc] == [
        ("Appliance", help_ + "Content/kb/siris-alto-nas/applianceLanding.htm"),
        ("Setup", help_ + "Content/kb/siris-alto-nas/setup.htm"),
    ]


@pytest.mark.asyncio
async def test_handles_extra_tree_keys_and_multi_position_pages():
    """Some Flare builds add extra bare keys to tree nodes (e.g. ``w``), and list
    a page at multiple TOC positions via parallel i/t lists
    (``i:[0,2],t:['A first','A again']``). Regression for the Datto Continuity
    bookshelf returning 0 entries (tree JSON parse failed on the ``w`` key)."""
    root = "https://c.example.com/help/Content/kb/x/foo.htm"
    help_ = "https://c.example.com/help/"
    raw = {
        help_ + "Data/HelpSystem.xml": '<WebHelpSystem Toc="Data/Tocs/M.js" />',
        help_ + "Data/Tocs/M.js":
            "define({numchunks:1,prefix:'M_Chunk',"
            "tree:{n:[{i:0,c:0,w:1},{i:1,c:0,f:'_self'},{i:2,c:0,w:1}]}})",
        help_ + "Data/Tocs/M_Chunk0.js":
            "define({'/Content/a.htm':{i:[0,2],t:['A first','A again'],b:['','']},"
            "'/Content/b.htm':{i:[1],t:['B'],b:['']}})",
    }
    scraper = FakeScraper({}, raw_by_url=raw)
    toc = await flare_helpsystem_toc(scraper, root)
    assert [(e.title, e.url) for e in toc] == [
        ("A first", help_ + "Content/a.htm"),
        ("B", help_ + "Content/b.htm"),
        ("A again", help_ + "Content/a.htm"),
    ]


@pytest.mark.asyncio
async def test_placeholder_book_nodes_become_url_less_sections():
    """MadCap '___' book nodes have no page of their own. They must become
    url-less sections (is_article=False) — NOT all collapse to one '/___' URL,
    which scrambled the tree (Datto BCDR: 113 sections merged into one)."""
    root = "https://c.example.com/help/Content/x/foo.htm"
    help_ = "https://c.example.com/help/"
    raw = {
        help_ + "Data/HelpSystem.xml": '<WebHelpSystem Toc="Data/Tocs/M.js" />',
        help_ + "Data/Tocs/M.js":
            "define({numchunks:1,prefix:'M_Chunk',"
            "tree:{n:[{i:0,c:0,n:[{i:1,c:0}]},{i:2,c:0,n:[{i:3,c:0}]}]}})",
        help_ + "Data/Tocs/M_Chunk0.js":
            # Two distinct sections share the '___' placeholder href (parallel
            # i/t lists); the real pages carry proper hrefs.
            "define({'___':{i:[0,2],t:['Section A','Section B'],b:['','']},"
            "'/Content/a1.htm':{i:[1],t:['Page A1'],b:['']},"
            "'/Content/b1.htm':{i:[3],t:['Page B1'],b:['']}})",
    }
    scraper = FakeScraper({}, raw_by_url=raw)
    toc = await flare_helpsystem_toc(scraper, root)
    assert [(e.title, e.level, e.url, e.is_article) for e in toc] == [
        ("Section A", 0, None, False),
        ("Page A1", 1, help_ + "Content/a1.htm", True),
        ("Section B", 0, None, False),
        ("Page B1", 1, help_ + "Content/b1.htm", True),
    ]


@pytest.mark.asyncio
async def test_resolves_root_for_default_htm_at_help_root():
    """WebHelp/TriPane entry (default.htm) sits AT the help root, not in Content/.

    The data files must be found via the document's own directory (./), not its
    parent (../). Regression for Arcserve-style WebHelp finding 0 pages.
    """
    root = "https://h.example.com/Bookshelf/HTML/SolG/default.htm"
    sol = "https://h.example.com/Bookshelf/HTML/SolG/"
    raw = {
        sol + "Data/HelpSystem.xml": '<WebHelpSystem Toc="Data/Tocs/G.js" />',
        sol + "Data/Tocs/G.js": "define({numchunks:1,prefix:'G_Chunk',tree:{n:[{i:7,c:0}]}})",
        sol + "Data/Tocs/G_Chunk0.js": "define({'/Topic_A.htm':{i:[7],t:['Topic A'],b:['']}})",
    }
    scraper = FakeScraper({}, raw_by_url=raw)
    toc = await flare_helpsystem_toc(scraper, root)
    assert [(e.title, e.url) for e in toc] == [("Topic A", sol + "Topic_A.htm")]


@pytest.mark.asyncio
async def test_returns_empty_when_data_files_absent():
    # No raw files served -> get_raw raises -> graceful [] so callers can fall back.
    scraper = FakeScraper({}, raw_by_url={})
    assert await flare_helpsystem_toc(scraper, ROOT) == []
