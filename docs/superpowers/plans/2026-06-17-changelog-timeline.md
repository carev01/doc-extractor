# Changelog Timeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reshape the consolidated changelog into a date-grouped historical timeline of page-level events — **added / changed / removed** — each clickable (added→article, changed→side-by-side diff, removed→preserved article).

**Architecture:** Record removals at extraction time via two new `Article` columns (`removed_at`, `removal_run_id`). The changelog route returns a newest-first event stream built from three sources via `UNION ALL` (added=`articles.created_at`, changed=`ArticleVersion`, removed=`articles.removed_at`). The frontend groups events by calendar date and wires the three click-throughs, reusing the existing `VersionOverlay` for side-by-side diffs.

**Tech Stack:** FastAPI, SQLAlchemy async, Alembic, Pydantic v2, PostgreSQL; React 19 + TypeScript + Vite.

## Global Constraints

- New Alembic migration `down_revision` = `c2d3e4f5a6b7` (current head).
- Tests use the async fixture pattern from `backend/tests/test_versions.py`: per-test `create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)`, `Base.metadata.drop_all`/`create_all`, `get_db` override, `httpx.AsyncClient`. `TEST_DATABASE_URL` = main URL with `/docextractor_test`. Run from `backend/` with `pytest`.
- Interpreter is `python3`; the test DB `docextractor_test` exists.
- New model columns must be declared on the model (so `create_all`/tests get them) AND mirrored in the migration.
- The changelog API field carrying the event time is renamed `extracted_at` → `timestamp`; the frontend must be updated in lockstep (Task 4).
- Frontend has no component test runner — verify with `npm run build` (type-check) + `npm run lint`; introduce no new lint errors.
- Branch context: this builds on the current branch state (includes the `toc_entry_id` re-link fix `411c486`).

---

### Task 1: `removed_at` / `removal_run_id` columns + migration

**Files:**
- Modify: `backend/app/models/article.py`
- Create: `backend/alembic/versions/d3e4f5a6b7c8_add_article_removal_tracking.py`

**Interfaces:**
- Produces: `Article.removed_at: datetime | None`, `Article.removal_run_id: uuid.UUID | None` (FK→`extraction_runs.id`, `ondelete=SET NULL`).

- [ ] **Step 1: Add the two columns to the `Article` model**

In `backend/app/models/article.py`, add these columns right after the existing `toc_entry_id` block (after line 33, before the `# Content` section):

```python
    # Removal tracking — stamped when the page first drops out of the rebuilt
    # TOC, cleared if it returns. Drives the changelog "removed" events.
    removed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    removal_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("extraction_runs.id", ondelete="SET NULL"),
        nullable=True,
    )
```

(`DateTime`, `ForeignKey`, `UUID`, `Mapped`, `mapped_column` are already imported in this file.)

- [ ] **Step 2: Write the migration**

Create `backend/alembic/versions/d3e4f5a6b7c8_add_article_removal_tracking.py`:

```python
"""add article removal tracking

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "d3e4f5a6b7c8"
down_revision: Union[str, Sequence[str], None] = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("articles", sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "articles",
        sa.Column(
            "removal_run_id", UUID(as_uuid=True),
            sa.ForeignKey("extraction_runs.id", ondelete="SET NULL"), nullable=True,
        ),
    )
    # Backfill: any article already orphaned (dropped from the TOC) is a removal
    # we never recorded — stamp it at its last-seen time so it shows up.
    op.execute(
        "UPDATE articles SET removed_at = extracted_at "
        "WHERE toc_entry_id IS NULL AND removed_at IS NULL"
    )


def downgrade() -> None:
    op.drop_column("articles", "removal_run_id")
    op.drop_column("articles", "removed_at")
```

- [ ] **Step 3: Apply the migration**

Run: `cd backend && alembic upgrade head`
Expected: completes without error; `alembic current` shows `d3e4f5a6b7c8`.

- [ ] **Step 4: Commit**

```bash
git add backend/app/models/article.py backend/alembic/versions/d3e4f5a6b7c8_add_article_removal_tracking.py
git commit -m "feat(db): add article removal-tracking columns"
```

---

### Task 2: Removal detection in extraction

**Files:**
- Modify: `backend/app/services/firecrawl.py`
- Test: `backend/tests/test_versions.py`

**Interfaces:**
- Consumes: `Article.removed_at`, `Article.removal_run_id` (Task 1).
- Produces: `FirecrawlService._reconcile_removals(db, source_id, run_id) -> None` — stamps newly-orphaned articles and clears re-added ones; called from `extract_source` before the run is marked COMPLETED.

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_versions.py` (it already imports `Vendor, DocumentationSource, Article, TOCEntry, ExtractionRun`, `select`, `firecrawl_service`, `RunStatus`, `datetime/timezone`):

```python
async def test_reconcile_removals_stamps_clears_and_pins(client):
    """Newly orphaned articles get removed_at/removal_run_id; the timestamp is
    pinned on later runs; a re-added page is cleared; linked pages stay NULL."""
    client, TestSession = client
    async with TestSession() as s:
        vendor = Vendor(name="RemVendor")
        s.add(vendor)
        await s.flush()
        source = DocumentationSource(
            vendor_id=vendor.id, name="RemSrc", base_url="https://rm.com"
        )
        s.add(source)
        await s.flush()
        run1 = ExtractionRun(source_id=source.id, status=RunStatus.RUNNING)
        run2 = ExtractionRun(source_id=source.id, status=RunStatus.RUNNING)
        s.add_all([run1, run2])
        await s.flush()
        toc = TOCEntry(
            source_id=source.id, title="Kept", url="https://rm.com/k",
            level=0, sort_order=0, is_article=True,
        )
        s.add(toc)
        await s.flush()

        def mk(title, url, toc_id):
            return Article(
                source_id=source.id, toc_entry_id=toc_id, title=title,
                source_url=url, content_markdown="x", content_hash="h",
                sort_order=0, estimated_tokens=1, content_size_bytes=1,
            )

        kept = mk("Kept", "https://rm.com/k", toc.id)       # linked → never removed
        gone = mk("Gone", "https://rm.com/g", None)          # orphaned → removed
        s.add_all([kept, gone])
        await s.commit()
        source_id, run1_id, run2_id = source.id, run1.id, run2.id
        kept_id, gone_id, toc_id = kept.id, gone.id, toc.id

    async def fetch(aid):
        async with TestSession() as s:
            a = (await s.execute(select(Article).where(Article.id == aid))).scalar_one()
            return a.removed_at, a.removal_run_id, a.toc_entry_id

    # Run 1 reconcile: 'gone' is stamped, 'kept' untouched.
    async with TestSession() as s:
        await firecrawl_service._reconcile_removals(s, source_id, run1_id)
    g_removed1, g_run1, _ = await fetch(gone_id)
    k_removed, _, _ = await fetch(kept_id)
    assert g_removed1 is not None and g_run1 == run1_id
    assert k_removed is None

    # Run 2 reconcile (still orphaned): timestamp + run pinned to first detection.
    async with TestSession() as s:
        await firecrawl_service._reconcile_removals(s, source_id, run2_id)
    g_removed2, g_run2, _ = await fetch(gone_id)
    assert g_removed2 == g_removed1 and g_run2 == run1_id

    # 'gone' is re-added (re-linked to a toc entry), then reconcile clears it.
    async with TestSession() as s:
        a = (await s.execute(select(Article).where(Article.id == gone_id))).scalar_one()
        a.toc_entry_id = toc_id
        await s.commit()
    async with TestSession() as s:
        await firecrawl_service._reconcile_removals(s, source_id, run2_id)
    g_removed3, g_run3, _ = await fetch(gone_id)
    assert g_removed3 is None and g_run3 is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_versions.py::test_reconcile_removals_stamps_clears_and_pins -v`
Expected: FAIL — `AttributeError: 'FirecrawlService' object has no attribute '_reconcile_removals'`.

- [ ] **Step 3: Implement `_reconcile_removals`**

In `backend/app/services/firecrawl.py`, add this method to the `FirecrawlService` class (place it just above `extract_source`). `update`, `Article`, `datetime`, `timezone` are already imported in this module:

```python
    async def _reconcile_removals(
        self, db: AsyncSession, source_id: uuid.UUID, run_id: uuid.UUID
    ) -> None:
        """Stamp pages that dropped out of the rebuilt TOC, clear ones that returned.

        Runs after all pages are processed (and re-linked), so the set of articles
        with toc_entry_id IS NULL is exactly the removed pages. removed_at is only
        set when currently NULL, so it stays pinned to first detection across runs.
        """
        now = datetime.now(timezone.utc)
        # Newly removed.
        await db.execute(
            update(Article)
            .where(
                Article.source_id == source_id,
                Article.toc_entry_id.is_(None),
                Article.removed_at.is_(None),
            )
            .values(removed_at=now, removal_run_id=run_id)
        )
        # Re-added → clear the removal flag.
        await db.execute(
            update(Article)
            .where(
                Article.source_id == source_id,
                Article.toc_entry_id.isnot(None),
                Article.removed_at.isnot(None),
            )
            .values(removed_at=None, removal_run_id=None)
        )
        await db.commit()
```

- [ ] **Step 4: Call it from `extract_source` at the finalize point**

In `backend/app/services/firecrawl.py`, find the success finalize block (right after `_poll_batch_and_process(...)` returns and before `run.status = RunStatus.COMPLETED`). Insert the reconcile call:

```python
            await self._poll_batch_and_process(
                db, source_id, run.id, url_to_entry, job_id, batch_tag=batch_tag
            )

            # Record removals (pages gone from the rebuilt TOC) before completing.
            await self._reconcile_removals(db, source_id, run.id)

            run.status = RunStatus.COMPLETED
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && pytest tests/test_versions.py::test_reconcile_removals_stamps_clears_and_pins -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/firecrawl.py backend/tests/test_versions.py
git commit -m "feat(extraction): record page removals at the finalize step"
```

---

### Task 3: Changelog schema + route (added / changed / removed union)

**Files:**
- Modify: `backend/app/schemas/version.py`
- Modify: `backend/app/routes/sources.py`
- Test: `backend/tests/test_versions.py`

**Interfaces:**
- Consumes: `Article.removed_at`, `Article.removal_run_id` (Task 1).
- Produces: `ChangelogEntry{article_id, title, change_type: str, timestamp: datetime, version_id: uuid|None, extraction_run_id: uuid|None, has_diff: bool}`; `GET /api/sources/{id}/changelog` returns these as a newest-first event stream over the three sources.

- [ ] **Step 1: Update the `ChangelogEntry` schema**

In `backend/app/schemas/version.py`, replace the `ChangelogEntry` class (currently `article_id, title, version_id, extraction_run_id, extracted_at, has_diff`) with:

```python
class ChangelogEntry(BaseModel):
    article_id: uuid.UUID
    title: str
    change_type: str  # "added" | "changed" | "removed"
    timestamp: datetime
    version_id: uuid.UUID | None
    extraction_run_id: uuid.UUID | None
    has_diff: bool
```

(`ChangelogResponse` is unchanged.)

- [ ] **Step 2: Write the failing test**

Add to `backend/tests/test_versions.py`:

```python
async def test_changelog_timeline_merges_added_changed_removed(client):
    """Changelog returns added (per article), changed (per version) and removed
    (per removed_at) events, newest-first, with the right discriminators."""
    client, TestSession = client
    T_OLD = datetime(2026, 1, 1, tzinfo=timezone.utc)
    T_MID = datetime(2026, 3, 1, tzinfo=timezone.utc)
    T_NEW = datetime(2026, 6, 1, tzinfo=timezone.utc)
    async with TestSession() as s:
        vendor = Vendor(name="ClVendor")
        s.add(vendor)
        await s.flush()
        source = DocumentationSource(
            vendor_id=vendor.id, name="ClSrc", base_url="https://cl.com"
        )
        s.add(source)
        await s.flush()
        run = ExtractionRun(source_id=source.id, status=RunStatus.COMPLETED)
        s.add(run)
        await s.flush()
        # Article A: added T_OLD, changed T_MID (one version snapshot).
        a = Article(
            source_id=source.id, toc_entry_id=None, title="Page A",
            source_url="https://cl.com/a", content_markdown="now", content_hash="h2",
            sort_order=0, estimated_tokens=1, content_size_bytes=1, created_at=T_OLD,
        )
        s.add(a)
        await s.flush()
        ver = ArticleVersion(
            article_id=a.id, extraction_run_id=run.id, content_markdown="old",
            content_hash="h1", diff_text="@@ -1 +1 @@", extracted_at=T_MID,
        )
        s.add(ver)
        # Article B: added T_OLD, removed T_NEW.
        b = Article(
            source_id=source.id, toc_entry_id=None, title="Page B",
            source_url="https://cl.com/b", content_markdown="x", content_hash="h",
            sort_order=0, estimated_tokens=1, content_size_bytes=1,
            created_at=T_OLD, removed_at=T_NEW, removal_run_id=run.id,
        )
        s.add(b)
        await s.commit()
        source_id = source.id

    r = await client.get(f"/api/sources/{source_id}/changelog")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 4  # 2 added + 1 changed + 1 removed
    types = [e["change_type"] for e in data["entries"]]
    # Newest-first: removed(T_NEW), changed(T_MID), then the two added(T_OLD).
    assert types[0] == "removed"
    assert types[1] == "changed"
    assert sorted(types[2:]) == ["added", "added"]
    removed = data["entries"][0]
    assert removed["version_id"] is None
    changed = data["entries"][1]
    assert changed["version_id"] is not None and changed["has_diff"] is True
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd backend && pytest tests/test_versions.py::test_changelog_timeline_merges_added_changed_removed -v`
Expected: FAIL — the current route only emits version (changed) events; `total` is 1 and `change_type` is absent.

- [ ] **Step 4: Rewrite the changelog route**

In `backend/app/routes/sources.py`, replace the body of `get_source_changelog` (keep the decorator/signature and the 404 source check) with a three-source union. `select`, `func`, `Article`, `ArticleVersion` are already imported; add `from sqlalchemy import literal, union_all` to the existing sqlalchemy import line (currently `from sqlalchemy import select, func`).

`NULL_VERSION_ID` is a typed NULL (cast to the version-id UUID type) so all three union branches agree on the column type — an untyped `NULL` first branch can otherwise trip Postgres' UNION type resolution. Define it just above the three selects:

```python
    null_version_id = literal(None).cast(ArticleVersion.id.type)

    # Three event streams sharing one column shape, merged newest-first.
    added = select(
        Article.id.label("article_id"),
        Article.title.label("title"),
        literal("added").label("change_type"),
        Article.created_at.label("timestamp"),
        null_version_id.label("version_id"),
        Article.extraction_run_id.label("extraction_run_id"),
        literal(False).label("has_diff"),
    ).where(Article.source_id == source_id)

    changed = select(
        ArticleVersion.article_id.label("article_id"),
        Article.title.label("title"),
        literal("changed").label("change_type"),
        ArticleVersion.extracted_at.label("timestamp"),
        ArticleVersion.id.label("version_id"),
        ArticleVersion.extraction_run_id.label("extraction_run_id"),
        ArticleVersion.diff_text.isnot(None).label("has_diff"),
    ).join(Article, Article.id == ArticleVersion.article_id).where(
        Article.source_id == source_id
    )

    removed = select(
        Article.id.label("article_id"),
        Article.title.label("title"),
        literal("removed").label("change_type"),
        Article.removed_at.label("timestamp"),
        null_version_id.label("version_id"),
        Article.removal_run_id.label("extraction_run_id"),
        literal(False).label("has_diff"),
    ).where(Article.source_id == source_id, Article.removed_at.isnot(None))

    events = union_all(added, changed, removed).subquery()
    total = (await db.execute(select(func.count()).select_from(events))).scalar()

    rows = await db.execute(
        select(events)
        .order_by(events.c.timestamp.desc())
        .offset(skip)
        .limit(limit)
    )

    entries = [
        ChangelogEntry(
            article_id=r.article_id,
            title=r.title,
            change_type=r.change_type,
            timestamp=r.timestamp,
            version_id=r.version_id,
            extraction_run_id=r.extraction_run_id,
            has_diff=r.has_diff,
        )
        for r in rows
    ]

    return ChangelogResponse(source_id=source_id, entries=entries, total=total)
```

Delete the old `count_query` / `rows` (single-source) block that this replaces.

- [ ] **Step 5: Run the test + the existing changelog test**

Run: `cd backend && pytest tests/test_versions.py -k changelog -v`
Expected: the new test PASSES. If `test_source_changelog_newest_first_across_articles` (the pre-existing changelog test) now fails because it asserts the old `extracted_at` field or a version-only `total`, update its assertions to the new shape: events use `timestamp` (not `extracted_at`), and `total` now includes one `added` event per article. Adjust only those assertions; keep the test's intent (changed events still appear newest-first).

- [ ] **Step 6: Run the full suite**

Run: `cd backend && pytest -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add backend/app/schemas/version.py backend/app/routes/sources.py backend/tests/test_versions.py
git commit -m "feat(api): changelog returns added/changed/removed timeline events"
```

---

### Task 4: Frontend timeline panel

**Files:**
- Modify: `frontend/src/types/index.ts`
- Modify: `frontend/src/components/ChangelogPanel.tsx`
- Modify: `frontend/src/App.css`

**Interfaces:**
- Consumes: `GET /api/sources/{id}/changelog` events with `change_type`, `timestamp`, nullable `version_id` (Task 3); existing `getArticle(id)`, `VersionOverlay({articleId, title, currentMarkdown, onClose})`, `MarkdownView({content})`.

- [ ] **Step 1: Update the `ChangelogEntry` TS type**

In `frontend/src/types/index.ts`, replace the `ChangelogEntry` interface with:

```typescript
export type ChangeType = "added" | "changed" | "removed";

export interface ChangelogEntry {
  article_id: string;
  title: string;
  change_type: ChangeType;
  timestamp: string;
  version_id: string | null;
  extraction_run_id: string | null;
  has_diff: boolean;
}
```

- [ ] **Step 2: Rewrite `ChangelogPanel` as a date-grouped timeline**

Replace the entire contents of `frontend/src/components/ChangelogPanel.tsx` with:

```tsx
import { useState, useEffect, useMemo } from "react";
import type { DocumentationSource, ChangelogEntry, ArticleDetail } from "../types";
import { getSourceChangelog, getArticle } from "../api/client";
import MarkdownView from "./MarkdownView";
import VersionOverlay from "./VersionOverlay";

interface Props {
  source: DocumentationSource;
}

const BADGE: Record<ChangelogEntry["change_type"], string> = {
  added: "ADDED",
  changed: "CHANGED",
  removed: "REMOVED",
};

function dateKey(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, {
    year: "numeric", month: "short", day: "numeric",
  });
}

export default function ChangelogPanel({ source }: Props) {
  const [entries, setEntries] = useState<ChangelogEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  // Open viewer: either a side-by-side version overlay (changed) or a rendered
  // article (added/removed).
  const [overlay, setOverlay] = useState<{ id: string; title: string; md: string } | null>(null);
  const [article, setArticle] = useState<{ detail: ArticleDetail; removed: boolean } | null>(null);

  useEffect(() => {
    setOverlay(null);
    setArticle(null);
    setLoading(true);
    setError("");
    getSourceChangelog(source.id)
      .then((d) => setEntries(d.entries))
      .catch(() => setError("Failed to load changelog"))
      .finally(() => setLoading(false));
  }, [source.id]);

  const groups = useMemo(() => {
    const m = new Map<string, ChangelogEntry[]>();
    for (const e of entries) {
      const k = dateKey(e.timestamp);
      (m.get(k) ?? m.set(k, []).get(k)!).push(e);
    }
    return Array.from(m.entries()); // insertion order = newest-first from API
  }, [entries]);

  const openEntry = async (e: ChangelogEntry) => {
    setError("");
    try {
      const detail = await getArticle(e.article_id);
      if (e.change_type === "changed") {
        setOverlay({ id: e.article_id, title: detail.title, md: detail.content_markdown });
      } else {
        setArticle({ detail, removed: e.change_type === "removed" });
      }
    } catch {
      setError("Failed to open article");
    }
  };

  if (source.status !== "completed") {
    return (
      <div className="changelog-panel">
        <p className="hint">
          Run an extraction first — the changelog records changes captured across runs.
        </p>
      </div>
    );
  }

  return (
    <div className="changelog-panel">
      <h2>Changelog — {source.name}</h2>
      <p className="hint">A timeline of page additions, changes and removals, newest first.</p>

      {error && <p className="error">{error}</p>}
      {loading && <p>Loading changelog…</p>}
      {!loading && entries.length === 0 && (
        <p className="hint">No events yet.</p>
      )}

      {groups.map(([day, evs]) => (
        <div key={day} className="timeline-group">
          <div className="timeline-date">{day}</div>
          <ul className="timeline-list">
            {evs.map((e, i) => (
              <li key={`${e.change_type}-${e.version_id ?? e.article_id}-${i}`} className="timeline-row">
                <button className="timeline-event" onClick={() => openEntry(e)}>
                  <span className={`badge-${e.change_type}`}>{BADGE[e.change_type]}</span>
                  <span className="timeline-title">{e.title}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      ))}

      {overlay && (
        <VersionOverlay
          articleId={overlay.id}
          title={overlay.title}
          currentMarkdown={overlay.md}
          onClose={() => setOverlay(null)}
        />
      )}

      {article && (
        <div className="article-modal-backdrop" onClick={() => setArticle(null)}>
          <div className="article-modal" onClick={(ev) => ev.stopPropagation()}>
            <div className="article-modal-head">
              <h3>{article.detail.title}</h3>
              <button onClick={() => setArticle(null)}>✕</button>
            </div>
            {article.removed && (
              <div className="removed-banner">
                This page is no longer present in the source's current table of
                contents. It is preserved here from the last run that included it.
              </div>
            )}
            <MarkdownView content={article.detail.content_markdown} />
          </div>
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Add timeline + modal styles**

In `frontend/src/App.css`, add styles consistent with the existing petrol-ink / signal-amber design system (match the existing `.changelog-*` and `.badge-*` patterns already in the file) for: `.timeline-group`, `.timeline-date`, `.timeline-list`, `.timeline-row`, `.timeline-event`, `.timeline-title`, `.badge-added`, `.badge-changed`, `.badge-removed`, `.article-modal-backdrop`, `.article-modal`, `.article-modal-head`. Reuse the existing `.removed-banner` rule (already defined for the docs browser). Color the badges by type (e.g. added=green accent, changed=amber, removed=muted/red), mirroring the existing `badge-new`/`badge-upd`/`badge-removed` treatment.

- [ ] **Step 4: Type-check, build, lint**

Run: `cd frontend && npm run build && npm run lint`
Expected: build succeeds with no type errors; lint introduces no new errors (the project has a known pre-existing lint baseline — compare against it).

- [ ] **Step 5: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/components/ChangelogPanel.tsx frontend/src/App.css
git commit -m "feat(ui): changelog timeline with added/changed/removed click-through"
```

---

## Self-Review

**Spec coverage:**
- Removal tracking columns + detection at finalize → Tasks 1, 2.
- Backfill of currently-orphaned articles → Task 1 migration.
- Changelog as added/changed/removed UNION, newest-first, paginated → Task 3.
- `change_type` discriminator, nullable `version_id`, `timestamp` field → Tasks 3, 4.
- Date-grouped timeline + three click-throughs (added→article, changed→side-by-side overlay, removed→preserved article) → Task 4.
- Testing (extraction detection incl. pin/clear; route union ordering/discriminators) → Tasks 2, 3; frontend build/lint → Task 4.

**Placeholder scan:** No TBD/TODO. The only directed-not-verbatim steps are Task 3 Step 5 (adjust the pre-existing changelog test's assertions to the new `timestamp`/`total` shape — the exact prior assertions aren't reproduced because they live in code the implementer will read) and Task 4 Step 3 (CSS to match an existing design system — concrete selector list given, visual values left to match surrounding rules). Both are intentional and bounded.

**Type consistency:** `change_type`/`timestamp`/`version_id|None` are identical across the schema (Task 3), the TS type (Task 4), and the route rows. `_reconcile_removals(db, source_id, run_id)` signature matches between Task 2's implementation, its call site, and its test. `VersionOverlay` is invoked with its real props (`articleId`, `title`, `currentMarkdown`, `onClose`); `getArticle`/`MarkdownView` used per their existing signatures.

## Out of scope (from the spec)
Per-run grouping/drill-down; renamed events; reorder/re-parent events; changelog export/RSS.
