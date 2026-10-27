"""One-off: split the Cohesity NetBackup source into one source per publication.

Cohesity's docs sidebar now lists only the publication the current page belongs
to, so the single NetBackup source — built when the nav was product-wide — would
re-extract as one publication. Rather than a per-product profile (a corner case
against the one-TOC-one-source design), the existing source is split in place:

* each of the 33 publications becomes its own ``collapsible_sidebar`` source with
  a ``{rev}`` template anchored on *its* publication id (the original template
  anchored only Release Notes, leaving 32 publications' keys with literal build
  ids that the next release would have re-keyed — the CommCell duplication);
* live articles move with their ``id``, content and dates intact;
* prior-release copies (removed by the 11.2 -> 11.2.0.1 bump) fold into the live
  article's version history;
* pages from publications that no longer exist are dropped.

Design: docs/superpowers/specs/2026-10-27-netbackup-source-split-design.md.
"""
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Article, DocumentationSource, Product
from app.models.toc import TOCEntry
from app.services.profiles.collapsible_sidebar import CollapsibleSidebarProfile
from app.services.versioning import VERSION_PLACEHOLDER, derive_topic_key

DEFAULT_SOURCE_ID = uuid.UUID("cb3a6f6a-f51e-414e-b2ec-64eb9a2217b5")
EXPECTED_VERSION = "11.2.0.1"
ORIGIN = "https://docs.cohesity.com/docs/netbackup"

_PUB_RE = re.compile(r"/docs/netbackup/[^/]+/(\d+)-(\d+)-\d+")


class SplitAborted(Exception):
    """A pre- or postcondition failed; nothing may be committed."""


@dataclass(frozen=True)
class Guide:
    pub: str
    build: str
    title: str


# Captured 2026-10-27 from https://docs.cohesity.com/docs/netbackup/11.2.0.1 (first
# link per publication); matches the live corpus' 33 publications and build ids.
GUIDES: tuple[Guide, ...] = (
    Guide('160052279', '173355268', 'Cohesity Cloud Scale Technology Manual Deployment Guide for Kubernetes Clusters'),
    Guide('170268462', '173432071', "Cohesity Data Cloud for NetBackup™ Administrator's Guide"),
    Guide('15883112', '173772466', "NetBackup Web UI Vault Administrator's Guide"),
    Guide('62428570', '173431998', 'NetBackup™ AdvancedDisk Storage Solutions Guide'),
    Guide('25435676', '173772175', 'NetBackup™ Backup, Archive, and Restore Getting Started Guide'),
    Guide('26437400', '173772194', "NetBackup™ Bare Metal Restore Administrator's Guide"),
    Guide('58500769', '173772301', "NetBackup™ Cloud Administrator's Guide"),
    Guide('24426936', '173432068', "NetBackup™ Clustered Primary Server Administrator's Guide"),
    Guide('42829485', '173523193', "NetBackup™ DataStore SDK Programmer's Guide for XBSA 1.1.0"),
    Guide('25074086', '173432076', 'NetBackup™ Deduplication Guide'),
    Guide('23395442', '173772203', 'NetBackup™ Device Configuration Guide'),
    Guide('81225970', '170177351', 'NetBackup™ Emergency Engineering Binary Guide'),
    Guide('16222484', '173772327', "NetBackup™ for DB2 Administrator's Guide"),
    Guide('16237876', '173355149', "NetBackup™ for HCL Domino Administrator's Guide"),
    Guide('170163529', '173611942', "NetBackup™ for KVM Administrator's Guide"),
    Guide('19475139', '173916939', "NetBackup™ for Microsoft Exchange Server Administrator's Guide"),
    Guide('166028765', '173772503', "NetBackup™ for Microsoft SQL Server Administrator's Guide"),
    Guide('150073865', '173785610', "NetBackup™ for Nutanix AHV Administrator's Guide"),
    Guide('156090006', '173772490', "NetBackup™ for PostgreSQL Administrator's Guide"),
    Guide('130392641', '173503649', "NetBackup™ for SQLite Administrator's Guide"),
    Guide('166027891', '173772441', "NetBackup™ for VMware Administrator's Guide"),
    Guide('86063237', '173772185', 'NetBackup™ Logging Reference Guide'),
    Guide('152708337', '173355271', 'NetBackup™ Marketplace Deployment on AWS'),
    Guide('156989604', '172152055', "NetBackup™ NAS Administrator's Guide"),
    Guide('59118460', '173432073', 'NetBackup™ OpenStorage Solutions Guide for Disk'),
    Guide('169433500', '172152080', 'NetBackup™ Plug-in for VMware vSphere Client (HTML5) Guide'),
    Guide('103228346', '173151032', 'NetBackup™ Release Notes'),
    Guide('21733320', '173772184', 'NetBackup™ Security and Encryption Guide'),
    Guide('140789355', '173355274', 'NetBackup™ Snapshot Manager for Cloud Install and Upgrade Guide'),
    Guide('155729295', '173772419', "NetBackup™ Snapshot Manager for Data Center Administrator's Guide"),
    Guide('44037985', '173488551', 'NetBackup™ Status Codes Reference Guide'),
    Guide('15179611', '173606293', 'NetBackup™ Troubleshooting Guide'),
    Guide('150157642', '173432023', "NetBackup™ Web UI Administrator's Guide"),
)


@dataclass
class NewSource:
    pub: str
    name: str
    base_url: str
    url_template: str


@dataclass
class SplitPlan:
    version: str
    product_id: uuid.UUID
    sources: dict[str, NewSource]
    # article id -> (publication, new topic_key)
    live: dict[uuid.UUID, tuple[str, str]] = field(default_factory=dict)
    # surviving article id -> rows folded into its history, oldest first
    history: dict[uuid.UUID, list[uuid.UUID]] = field(default_factory=dict)
    # removed survivors that move as removed rows -> (publication, new topic_key)
    removed_moves: dict[uuid.UUID, tuple[str, str]] = field(default_factory=dict)
    retired: list[uuid.UUID] = field(default_factory=list)
    # toc entry id -> publication, or None (stays; deleted with the original)
    toc: dict[uuid.UUID, str | None] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        return {
            "live": len(self.live),
            "folded": sum(len(v) for v in self.history.values()),
            "removed_moves": len(self.removed_moves),
            "retired": len(self.retired),
            "toc_moved": sum(1 for p in self.toc.values() if p is not None),
            "toc_left": sum(1 for p in self.toc.values() if p is None),
        }


def _pub(url: str | None) -> str | None:
    m = _PUB_RE.search(url or "")
    return m.group(1) if m else None


async def build_split_plan(
    db: AsyncSession, source_id: uuid.UUID, guides: Sequence[Guide] = GUIDES
) -> SplitPlan:
    """Classify every row of *source_id*; reads only."""
    src = await db.get(DocumentationSource, source_id)
    if src is None:
        raise SplitAborted(f"source {source_id} not found")
    product = await db.get(Product, src.product_id)
    version = product.version
    if version != EXPECTED_VERSION:
        raise SplitAborted(f"product version is {version!r}, expected {EXPECTED_VERSION!r}")

    profile = CollapsibleSidebarProfile()
    sources: dict[str, NewSource] = {}
    for g in guides:
        base_url = f"{ORIGIN}/{version}/{g.pub}-{g.build}-0"
        template = profile.templatize_url(base_url, version)
        if not template:
            raise SplitAborted(f"cannot templatize {base_url}")
        sources[g.pub] = NewSource(g.pub, g.title, base_url, template)
    plan = SplitPlan(version=version, product_id=product.id, sources=sources)

    rows = (await db.execute(
        select(Article.id, Article.source_url, Article.topic_key,
               Article.removed_at, Article.extracted_at)
        .where(Article.source_id == source_id)
    )).all()

    def key(url: str, pub: str) -> str:
        return derive_topic_key(url, sources[pub].url_template, version)

    live_by_key: dict[tuple[str, str], uuid.UUID] = {}
    for r in rows:
        if r.removed_at is not None:
            continue
        pub = _pub(r.source_url)
        if pub not in sources:
            raise SplitAborted(f"live article {r.id} is in publication {pub}, not a known guide")
        k = key(r.source_url, pub)
        if (pub, k) in live_by_key:
            raise SplitAborted(f"live articles {live_by_key[(pub, k)]} and {r.id} derive the same key {k}")
        live_by_key[(pub, k)] = r.id
        plan.live[r.id] = (pub, k)

    # Removed rows: source_url still carries the release they were removed in, so
    # the identity is rebuilt from the stored (version-templated) key instead.
    twins: dict[uuid.UUID, list] = defaultdict(list)
    orphans: dict[tuple[str, str], list] = defaultdict(list)
    for r in rows:
        if r.removed_at is None:
            continue
        url = r.topic_key.replace(VERSION_PLACEHOLDER, version)
        pub = _pub(url)
        if pub not in sources:
            plan.retired.append(r.id)
            continue
        k = key(url, pub)
        if (pub, k) in live_by_key:
            twins[live_by_key[(pub, k)]].append(r)
        else:
            orphans[(pub, k)].append(r)

    for live_id, group in twins.items():
        plan.history[live_id] = [r.id for r in sorted(group, key=lambda r: r.extracted_at)]
    for (pub, k), group in orphans.items():
        group = sorted(group, key=lambda r: r.extracted_at)
        survivor = group[-1]
        plan.removed_moves[survivor.id] = (pub, k)
        if len(group) > 1:
            plan.history[survivor.id] = [r.id for r in group[:-1]]

    toc_rows = (await db.execute(
        select(TOCEntry.id, TOCEntry.url, TOCEntry.parent_id).where(TOCEntry.source_id == source_id)
    )).all()
    children: dict[uuid.UUID, list] = defaultdict(list)
    for t in toc_rows:
        if t.parent_id is not None:
            children[t.parent_id].append(t)
    memo: dict[uuid.UUID, set[str]] = {}

    def pubs_below(t) -> set[str]:
        if t.id not in memo:
            found = {_pub(t.url)} if t.url else set()
            for c in children[t.id]:
                found |= pubs_below(c)
            memo[t.id] = found
        return memo[t.id]

    for t in toc_rows:
        if t.url:
            pub = _pub(t.url)
            plan.toc[t.id] = pub if pub in sources else None
        else:
            # A heading follows its subtree only when that subtree is one guide.
            below = pubs_below(t)
            plan.toc[t.id] = next(iter(below)) if len(below) == 1 and below <= sources.keys() else None
    return plan
