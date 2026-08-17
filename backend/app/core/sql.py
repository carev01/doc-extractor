"""SQL helpers for queries whose parameters scale with the data.

asyncpg (PostgreSQL's extended query protocol) hard-caps a statement at **32767
bind parameters**. A SQL ``IN`` list binds one parameter *per element*, so any
``col.in_(collection)`` where the collection grows with the data eventually raises::

    asyncpg.exceptions._base.InterfaceError:
        the number of query arguments cannot exceed 32767

That is a latent time bomb rather than a visible bug: it works in dev and on small
sources, then starts failing once a source (or the media volume) crosses the cap.
It took down the media GC in production — every maintenance sweep raised, so
orphaned media was never collected.

``any_of`` expresses the same predicate as ``= ANY(:arr)``, binding the whole
collection as **one array parameter**. It is therefore bounded by total payload
size rather than element count, keeps the query a single round trip, and uses the
same index as ``IN``.
"""

from collections.abc import Iterable
from typing import Any

from sqlalchemy import ColumnElement, bindparam, false, func
from sqlalchemy.dialects.postgresql import ARRAY

# asyncpg / libpq extended-protocol ceiling on bind parameters per statement.
BIND_PARAM_LIMIT = 32767


def any_of(column: Any, values: Iterable[Any]) -> ColumnElement[bool]:
    """Return ``column = ANY(:values)`` — an IN-equivalent using one bind param.

    Prefer this over ``column.in_(values)`` whenever ``values`` can scale with the
    data (article ids, TOC entry ids, ids read off disk). The element type is
    derived from ``column``, so UUID/text/int columns all work.

    An empty collection yields a false predicate, matching ``in_([])`` semantics
    (no rows), without emitting an untyped empty-array literal.
    """
    vals = list(values)
    if not vals:
        return false()
    return column == func.any_(
        bindparam(None, value=vals, type_=ARRAY(column.type), unique=True)
    )
