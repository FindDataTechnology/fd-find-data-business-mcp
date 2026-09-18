"""Law-domain tools over the federated law_db (finddata-branded).

Metadata-only title search (trigram-indexed, ranked exact > prefix >
contains) plus full-content single-law reads. Provenance columns
(src_db/src_id/source_id/file_path/quality_flag/content_status) never
leave this module; every payload carries ``brand: "finddata"``.
"""
from __future__ import annotations

from typing import Any

from fd_find_data_business_mcp.domain import query

_BRAND = "finddata"

_FORBIDDEN_KEYS = frozenset({
    "source", "source_used", "real_source_used", "real_source",
    "src_db", "src_id", "source_id", "file_path",
    "content_status", "quality_flag",
})

_METADATA_FIELDS = ("id", "title", "category", "type", "status", "publish", "expiry")

_MAX_LIMIT = 200


def _escape_like(q: str) -> str:
    """Escape LIKE/ILIKE metacharacters so user input matches literally."""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _rank(title: str | None, needle: str) -> int:
    """0 exact, 1 prefix, 2 contains — mirrors the SQL CASE ranking."""
    t = (title or "").lower()
    if t == needle:
        return 0
    if t.startswith(needle):
        return 1
    return 2


def _result(row: dict[str, Any], *, content: bool = False) -> dict[str, Any]:
    out = {k: row.get(k) for k in _METADATA_FIELDS}
    if content:
        out["content"] = row.get("content")
    out["brand"] = _BRAND
    return {k: v for k, v in out.items() if k not in _FORBIDDEN_KEYS}


def law_search(title_query: str, category: str | None = None, limit: int = 50) -> dict[str, Any]:
    """Search FindData law titles, ranked exact > prefix > contains.

    Case-insensitive substring match on a trigram index, optional exact
    category filter, at most 200 rows. Returns ``{results: [metadata +
    brand], count, truncated}`` — law content is never included.
    """
    q = (title_query or "").strip()
    if not q:
        return {"results": [], "count": 0, "truncated": False}
    limit = min(max(limit, 1), _MAX_LIMIT)
    esc = _escape_like(q)
    params: dict[str, Any] = {
        "pattern": f"%{esc}%",
        "exact": esc,
        "prefix": f"{esc}%",
        "lim": limit + 1,
    }
    sql = (
        "SELECT id, title, category, type, status, publish, expiry\n"
        "FROM laws\n"
        "WHERE title ILIKE :pattern\n"
    )
    if category:
        sql += "AND category = :category\n"
        params["category"] = category
    sql += (
        "ORDER BY CASE WHEN title ILIKE :exact THEN 0\n"
        "             WHEN title ILIKE :prefix THEN 1\n"
        "             ELSE 2 END, title\n"
        "LIMIT :lim"
    )
    rows = query("law_db", sql, params)
    if isinstance(rows, dict):
        return rows
    needle = q.lower()
    rows = sorted(rows, key=lambda r: (_rank(r.get("title"), needle), r.get("title") or ""))
    truncated = len(rows) > limit
    return {
        "results": [_result(r) for r in rows[:limit]],
        "count": min(len(rows), limit),
        "truncated": truncated,
    }


def law_read(law_id: int) -> dict[str, Any]:
    """Read one FindData law by id, full content included.

    An unknown id returns ``{"status": "not_found"}``; a federation
    failure passes through as ``{"status": "domain_unavailable"}``.
    """
    rows = query(
        "law_db",
        "SELECT id, title, category, type, status, publish, expiry, content\n"
        "FROM laws\n"
        "WHERE id = :law_id",
        {"law_id": law_id},
    )
    if isinstance(rows, dict):
        return rows
    if not rows:
        return {"status": "not_found", "brand": _BRAND}
    return _result(rows[0], content=True)
