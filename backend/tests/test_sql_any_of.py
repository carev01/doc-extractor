"""``any_of`` must express an IN-equivalent with ONE bind parameter.

asyncpg hard-caps a statement at 32767 bind parameters, and ``col.in_(collection)``
binds one per element — so any IN over a collection that scales with the data
eventually raises InterfaceError. That took down the media GC in production. These
tests pin the invariant that actually prevents it (parameter *count*), so they stay
fast and need no database.
"""

import os
import sys
import uuid

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.sql import BIND_PARAM_LIMIT, any_of
from app.models.article import Article


def _compiled(expr):
    # render_postcompile expands SQLAlchemy's "expanding" IN placeholder into the
    # individual parameters the driver actually receives. Without it an IN list
    # also reports a single param, hiding the very difference under test (which is
    # why this bug reached production: it only surfaces at execution).
    return (
        select(Article.id)
        .where(expr)
        .compile(dialect=postgresql.dialect(), compile_kwargs={"render_postcompile": True})
    )


def test_binds_a_single_parameter_regardless_of_size():
    # The whole point: 50k values must still be ONE parameter, not 50k.
    ids = [uuid.uuid4() for _ in range(50_000)]
    assert len(ids) > BIND_PARAM_LIMIT
    assert len(_compiled(any_of(Article.id, ids)).params) == 1


def test_in_list_would_exceed_the_cap_for_the_same_input():
    # Contrast case documenting why any_of exists — one param per element, which is
    # what asyncpg rejects past 32767.
    ids = [uuid.uuid4() for _ in range(50_000)]
    assert len(_compiled(Article.id.in_(ids)).params) == len(ids) > BIND_PARAM_LIMIT


def test_renders_as_equals_any():
    c = _compiled(any_of(Article.id, [uuid.uuid4()]))
    sql = str(c).replace("\n", " ")
    assert "= ANY (" in sql.upper() or "= ANY(" in sql.upper()


def test_empty_collection_yields_no_rows_and_no_params():
    c = _compiled(any_of(Article.id, []))
    sql = str(c).upper()
    assert "ANY" not in sql          # no untyped empty-array literal
    assert "FALSE" in sql            # matches in_([]) semantics: no rows


def test_accepts_any_iterable_not_just_list():
    # media_gc passes a dict (iterating keys); a generator must work too.
    as_dict = {uuid.uuid4(): "/path" for _ in range(3)}
    assert len(_compiled(any_of(Article.id, as_dict)).params) == 1
    assert len(_compiled(any_of(Article.id, (u for u in [uuid.uuid4()]))).params) == 1


def test_derives_element_type_from_the_column():
    # A non-UUID column must bind a correctly-typed array, so the helper is safe to
    # reuse beyond article ids.
    c = _compiled(any_of(Article.topic_key, ["a", "b"]))
    assert len(c.params) == 1
    assert "ANY" in str(c).upper()
