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
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import bindparam, delete, func, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sql import any_of
from app.models import Article, ArticleVersion, DocumentationSource, ExtractionRun, Product
from app.models.content_change import ChangeType, ContentChange
from app.models.toc import TOCEntry
from app.services import change_log
from app.services.delta_feed import _ACTIVE
from app.services.profiles.collapsible_sidebar import CollapsibleSidebarProfile
from app.services.versioning import VERSION_PLACEHOLDER, derive_topic_key

_BATCH = 1000

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


@dataclass
class SplitResult:
    source_ids: dict[str, uuid.UUID]
    moved: int
    versions_added: int
    versions_repointed: int
    deleted: int
    toc_moved: int
    outbox: int


def _batches(items: list, n: int = _BATCH):
    for i in range(0, len(items), n):
        yield items[i:i + n]


async def _executemany(db: AsyncSession, stmt, params: list[dict]) -> None:
    for chunk in _batches(params):
        await db.execute(stmt, chunk)


async def apply_split(db: AsyncSession, source_id: uuid.UUID, plan: SplitPlan) -> SplitResult:
    """Perform the split described by *plan* in the caller's transaction.

    Never commits: every postcondition is checked here and a failure raises
    SplitAborted, so the caller rolls back and nothing is left half-split. Bulk
    writes go through Core executemany on the tables (an ORM bulk update by a
    parameter list keys only on the primary key and has bitten this codebase)."""
    # Same lock as other out-of-band outbox writers, so outbox ids cannot
    # interleave with a concurrent source deletion's commit order.
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": change_log._DELETION_LOCK_KEY})

    active = (await db.execute(
        select(func.count()).select_from(ExtractionRun).where(ExtractionRun.status.in_(_ACTIVE))
    )).scalar_one()
    if active:
        raise SplitAborted(f"{active} active extraction run(s); refusing to split during a run")
    original = await db.get(DocumentationSource, source_id)
    live_left = (await db.execute(
        select(func.count()).select_from(Article)
        .where(Article.source_id == source_id, Article.removed_at.is_(None))
    )).scalar_one()
    if not live_left:
        raise SplitAborted("original source has no live articles — already split?")
    names = [ns.name for ns in plan.sources.values()]
    clash = (await db.execute(
        select(DocumentationSource.name).where(
            DocumentationSource.product_id == plan.product_id, DocumentationSource.name.in_(names))
    )).scalars().all()
    if clash:
        raise SplitAborted(f"sources already exist under the product: {sorted(clash)[:3]}")
    fresh = await build_split_plan(db, source_id, [
        Guide(ns.pub, ns.base_url.rsplit("-", 2)[-2], ns.name) for ns in plan.sources.values()])
    if fresh.counts() != plan.counts():
        raise SplitAborted(f"corpus drifted since planning: {plan.counts()} -> {fresh.counts()}")

    live_ids = list(plan.live)
    changed_before = dict((await db.execute(
        select(Article.id, Article.content_changed_at).where(any_of(Article.id, live_ids))
    )).all())

    # 1. The new sources.
    source_ids: dict[str, uuid.UUID] = {}
    for pub, ns in plan.sources.items():
        new = DocumentationSource(
            product_id=plan.product_id, name=ns.name, base_url=ns.base_url,
            url_template=ns.url_template, platform="collapsible_sidebar",
            source_type=original.source_type, profile_config=original.profile_config,
            auth_realm_id=original.auth_realm_id,
        )
        db.add(new)
        await db.flush()
        source_ids[pub] = new.id

    # 2. TOC entries follow their guide; one whose parent stayed behind (a
    # cross-guide root) becomes a root, or ChapterResolver — which only loads the
    # article's own source's entries — would walk to a parent it cannot see.
    toc = TOCEntry.__table__
    parents = dict((await db.execute(
        select(TOCEntry.id, TOCEntry.parent_id).where(TOCEntry.source_id == source_id)
    )).all())
    moves = [{"_id": tid, "_src": source_ids[pub],
              "_parent": parents[tid] if parents[tid] is not None and plan.toc.get(parents[tid]) == pub else None}
             for tid, pub in plan.toc.items() if pub is not None]
    await _executemany(db, update(toc).where(toc.c.id == bindparam("_id"))
                       .values(source_id=bindparam("_src"), parent_id=bindparam("_parent")), moves)

    # 3. Fold prior copies into the survivor's history, oldest first.
    art, ver = Article.__table__, ArticleVersion.__table__
    survivors = list(plan.history)
    folded_all = [fid for ids in plan.history.values() for fid in ids]
    repointed = added = 0
    for chunk in _batches(survivors, 500):
        folded = [fid for sid in chunk for fid in plan.history[sid]]
        rows = {r.id: r for r in (await db.execute(
            select(art.c.id, art.c.content_markdown, art.c.content_hash, art.c.source_url,
                   art.c.extracted_at, art.c.extraction_run_id).where(any_of(art.c.id, folded))
        )).all()}
        current = dict((await db.execute(
            select(art.c.id, art.c.content_hash).where(any_of(art.c.id, chunk)))).all())
        repoint = [{"_old": fid, "_new": sid} for sid in chunk for fid in plan.history[sid]]
        res = await db.execute(select(func.count()).select_from(ver).where(any_of(ver.c.article_id, folded)))
        repointed += res.scalar_one()
        await _executemany(db, update(ver).where(ver.c.article_id == bindparam("_old"))
                           .values(article_id=bindparam("_new")), repoint)
        chain: dict[uuid.UUID, list[tuple[datetime, str | None]]] = defaultdict(list)
        for aid, at, h in (await db.execute(
            select(ver.c.article_id, ver.c.extracted_at, ver.c.content_hash)
            .where(any_of(ver.c.article_id, chunk)))).all():
            chain[aid].append((at, h))
        new_versions = []
        for sid in chunk:
            for fid in plan.history[sid]:
                r = rows[fid]
                versions = sorted(chain[sid], key=lambda v: v[0])
                before = [v for v in versions if v[0] < r.extracted_at]
                is_newest = not any(v[0] > r.extracted_at for v in versions)
                if (before and before[-1][1] == r.content_hash) or (
                        is_newest and r.content_hash == current[sid]):
                    continue
                chain[sid].append((r.extracted_at, r.content_hash))
                new_versions.append({
                    "id": uuid.uuid4(), "article_id": sid, "extraction_run_id": r.extraction_run_id,
                    "content_markdown": r.content_markdown, "content_hash": r.content_hash,
                    "source_url": r.source_url, "extracted_at": r.extracted_at,
                })
        if new_versions:
            await _executemany(db, insert(ver), new_versions)
            added += len(new_versions)
    for chunk in _batches(folded_all, 5000):
        await db.execute(delete(art).where(any_of(art.c.id, chunk)))

    # 4. Live articles and removed survivors move with a key under their guide's template.
    article_moves = [{"_id": aid, "_src": source_ids[pub], "_key": k}
                     for aid, (pub, k) in {**plan.live, **plan.removed_moves}.items()]
    await _executemany(db, update(art).where(art.c.id == bindparam("_id"))
                       .values(source_id=bindparam("_src"), topic_key=bindparam("_key")), article_moves)

    # 5. Pages of retired publications (already tombstoned on the feed when removed).
    for chunk in _batches(plan.retired, 5000):
        await db.execute(delete(art).where(any_of(art.c.id, chunk)))

    # 6. One 'updated' outbox row per moved live article, written directly rather
    # than through change_log.record_change: a move is not a content mutation, so
    # content_changed_at must not move (graph-rag dates facts by it).
    now = datetime.now(timezone.utc)
    hashes = dict((await db.execute(
        select(art.c.id, art.c.content_hash).where(any_of(art.c.id, live_ids)))).all())
    outbox = [{"article_id": aid, "source_id": source_ids[pub], "run_id": None,
               "change_type": ChangeType.UPDATED.value, "content_hash": hashes[aid],
               "topic_key": k, "created_at": now} for aid, (pub, k) in plan.live.items()]
    await _executemany(db, insert(ContentChange.__table__), outbox)

    # Postconditions.
    new_ids = list(source_ids.values())
    if (await db.execute(select(func.count()).select_from(art).where(
            art.c.source_id == source_id, art.c.removed_at.is_(None)))).scalar_one():
        raise SplitAborted("postcondition: original source still has live articles")
    moved_live = (await db.execute(select(func.count()).select_from(art).where(
        any_of(art.c.source_id, new_ids), art.c.removed_at.is_(None)))).scalar_one()
    if moved_live != len(plan.live):
        raise SplitAborted(f"postcondition: {moved_live} live articles in new sources, expected {len(plan.live)}")
    dupes = (await db.execute(select(func.count()).select_from(
        select(art.c.source_id, art.c.topic_key).where(any_of(art.c.source_id, new_ids))
        .group_by(art.c.source_id, art.c.topic_key).having(func.count() > 1).subquery()))).scalar_one()
    if dupes:
        raise SplitAborted(f"postcondition: {dupes} duplicate (source_id, topic_key) pairs")
    lacking = (await db.execute(select(func.count()).select_from(art).where(
        any_of(art.c.id, [s for s in survivors if s in plan.live]),
        ~select(ver.c.id).where(ver.c.article_id == art.c.id).exists()))).scalar_one()
    if lacking:
        raise SplitAborted(f"postcondition: {lacking} live articles with folded history have no version")
    changed_after = dict((await db.execute(
        select(art.c.id, art.c.content_changed_at).where(any_of(art.c.id, live_ids)))).all())
    if changed_after != changed_before:
        raise SplitAborted("postcondition: content_changed_at moved on a moved article")

    return SplitResult(
        source_ids=source_ids, moved=len(plan.live), versions_added=added,
        versions_repointed=repointed, deleted=len(folded_all) + len(plan.retired),
        toc_moved=len(moves), outbox=len(outbox),
    )


async def run(db_url: str, source_id: uuid.UUID, apply: bool,
              guides: Sequence[Guide] = GUIDES) -> SplitResult:
    """Plan and perform the split. A dry run (``apply=False``) executes the real
    apply — every write and postcondition — and then rolls back."""
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.pool import NullPool

    url = make_url(db_url)
    print(f"target: {url.host}:{url.port}/{url.database}  mode: {'APPLY' if apply else 'dry run'}")
    engine = create_async_engine(db_url, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as db:
            plan = await build_split_plan(db, source_id, guides)
            print("plan:", plan.counts())
            try:
                result = await apply_split(db, source_id, plan)
            except BaseException:
                await db.rollback()
                raise
            if apply:
                await db.commit()
            else:
                await db.rollback()
            print("result:", {k: v for k, v in result.__dict__.items() if k != "source_ids"},
                  f"({len(result.source_ids)} sources)", "COMMITTED" if apply else "ROLLED BACK")
            return result
    finally:
        await engine.dispose()


if __name__ == "__main__":
    import argparse
    import asyncio
    import sys

    from app.core.config import settings

    ap = argparse.ArgumentParser(description="Split the NetBackup source per publication (one-off).")
    ap.add_argument("--db-url", default=settings.database_url)
    ap.add_argument("--source-id", type=uuid.UUID, default=DEFAULT_SOURCE_ID)
    ap.add_argument("--apply", action="store_true", help="commit (default: dry run, rolled back)")
    args = ap.parse_args()
    try:
        asyncio.run(run(args.db_url, args.source_id, args.apply))
    except SplitAborted as exc:
        print(f"ABORTED: {exc}", file=sys.stderr)
        sys.exit(1)
