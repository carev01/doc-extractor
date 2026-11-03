"""Give each DB-backed test an empty database without rebuilding the schema.

Dropping and recreating every table around each test cost ~0.5-0.9s of DDL per
test — about half the suite's wall time. The schema is now built once per
session per database (a real drop_all + create_all, so a stale schema from an
earlier run is still replaced) and every later reset is a single TRUNCATE.

A test that changes the schema itself (creates or drops an index) must call
``schema_changed()`` so the next reset rebuilds it instead of truncating.
"""
from sqlalchemy import text

from app.core.database import Base

_built: set[str] = set()


def reset_schema(conn) -> None:
    """Empty every table on *conn* (a sync Connection; use ``run_sync`` from async)."""
    key = conn.engine.url.render_as_string(hide_password=True)
    if key not in _built:
        Base.metadata.drop_all(conn)
        Base.metadata.create_all(conn)
        _built.add(key)
        return
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


def schema_changed() -> None:
    """The schema no longer matches the models; rebuild it on the next reset."""
    _built.clear()


def reset_schema_sync(engine) -> None:
    with engine.begin() as conn:
        reset_schema(conn)
