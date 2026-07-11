"""Delta-feed cursor codec + JSONL record field reference.

The cursor is an opaque, versioned wrapper over the content_changes watermark
(BIGSERIAL id). It reuses the base64-JSON codec from schemas.search so all
opaque cursors in the app share one encoding.

Record shapes emitted by the feed (as plain dicts, one JSON object per line):

CONTENT (change_type "added" | "updated"):
    seq, change_type, id, topic_key, source_id, vendor, product, title,
    source_url, last_updated_at, content_hash, estimated_tokens,
    parent_chapter, top_level_chapter, sort_order, run_id,
    content_markdown, images:[{url, alt, description, kind}]
    (In bootstrap mode seq is null — snapshot rows are not change events.)

TOMBSTONE (change_type "removed"):
    seq, change_type, id, topic_key, source_id, removed_at, run_id

CONTROL (always last line):
    control:"cursor", next_since, count
"""

from app.schemas.search import decode_cursor, encode_cursor

_CURSOR_VERSION = 1


def encode_delta_cursor(seq: int) -> str:
    """Encode a watermark seq as an opaque delta cursor."""
    return encode_cursor({"v": _CURSOR_VERSION, "seq": int(seq)})


def decode_delta_cursor(cursor: str) -> int:
    """Decode a delta cursor to its watermark seq.

    Raises ValueError if the token is malformed or not version 1.
    """
    payload = decode_cursor(cursor)  # raises ValueError on malformed base64/JSON
    if payload.get("v") != _CURSOR_VERSION or "seq" not in payload:
        raise ValueError("Unrecognized delta cursor")
    try:
        return int(payload["seq"])
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid delta cursor seq") from exc
