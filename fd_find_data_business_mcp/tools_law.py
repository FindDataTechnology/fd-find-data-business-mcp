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

# The crawled corpus stores crawler-batch labels (重下载/新法速递) and synonym
# spellings (地方法规/地方性法规) in the same category column. Customers see
# only the canonical vocabulary; batch labels never win a duplicate group and
# are not filterable (law-corpus-hygiene design D2/D3).
_BATCH_CATEGORIES = frozenset({"重下载", "新法速递"})
_CATEGORY_SYNONYMS = {"地方法规": "地方性法规"}
_FILTER_EXPANSION: dict[str, set[str]] = {}
for _syn, _canon in _CATEGORY_SYNONYMS.items():
    _FILTER_EXPANSION.setdefault(_canon, {_canon}).add(_syn)


def _normalize_category(category: str | None) -> str | None:
    """Map a synonym spelling to its canonical category (else pass through)."""
    if not category:
        return category
    return _CATEGORY_SYNONYMS.get(category, category)


def _category_class(row: dict[str, Any]) -> int:
    """0 = real-law category, 1 = crawler-batch label."""
    return 1 if (row.get("category") or "") in _BATCH_CATEGORIES else 0


def _status_class(row: dict[str, Any]) -> int:
    """0 = currently effective, 1 = anything else (incl. crawler states)."""
    return 0 if (row.get("status") or "") == "有效" else 1


def _publish_key(row: dict[str, Any]) -> tuple[bool, str]:
    """Descending-ready publish sort key: newest first, NULL last."""
    pub = row.get("publish")
    return (pub is None, str(pub or ""))


def _dedupe_authoritative(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one row per exact title: canonical category > batch category,
    then 有效, then newest publish. Safety net over the SQL DISTINCT ON —
    idempotent when the database already did the work."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault((r.get("title") or "").strip().lower(), []).append(r)
    out: list[dict[str, Any]] = []
    for group in groups.values():
        by_publish = sorted(group, key=_publish_key, reverse=True)
        out.append(min(by_publish, key=lambda r: (_category_class(r), _status_class(r))))
    return out


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
    out["category"] = _normalize_category(out.get("category"))
    if content:
        out["content"] = row.get("content")
    out["brand"] = _BRAND
    return {k: v for k, v in out.items() if k not in _FORBIDDEN_KEYS}


def law_search(title_query: str, category: str | None = None, limit: int = 50) -> dict[str, Any]:
    """Search FindData law titles, ranked exact > prefix > contains.

    Case-insensitive substring match on a trigram index, optional category
    filter (synonym spellings accepted; crawler-batch categories match
    nothing), at most 200 rows. One authoritative row per title: real-law
    category beats a crawler-batch label, then 有效 status, then newest
    publish — dedup happens before the size bound. Returns ``{results:
    [metadata + brand], count, truncated}`` — law content is never included.
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
        "batch": sorted(_BATCH_CATEGORIES),
    }
    sql = (
        "SELECT id, title, category, type, status, publish, expiry\n"
        "FROM (\n"
        "  SELECT DISTINCT ON (tkey)\n"
        "         id, title, category, type, status, publish, expiry, tkey, rank\n"
        "  FROM (\n"
        "    SELECT id, title, category, type, status, publish, expiry,\n"
        "           lower(title) AS tkey,\n"
        "           CASE WHEN title ILIKE :exact THEN 0\n"
        "                WHEN title ILIKE :prefix THEN 1\n"
        "                ELSE 2 END AS rank,\n"
        "           CASE WHEN category = ANY(:batch) THEN 1 ELSE 0 END AS cclass,\n"
        "           CASE WHEN status = '有效' THEN 0 ELSE 1 END AS sclass\n"
        "    FROM laws\n"
        "    WHERE title ILIKE :pattern\n"
    )
    if category:
        canon = _normalize_category(category.strip())
        if canon in _BATCH_CATEGORIES:
            # Batch labels are crawler bookkeeping, not a customer category.
            return {"results": [], "count": 0, "truncated": False}
        params["categories"] = sorted(_FILTER_EXPANSION.get(canon, {canon}))
        sql += "    AND category = ANY(:categories)\n"
    sql += (
        "  ) cand\n"
        "  ORDER BY tkey, rank, cclass, sclass, publish DESC NULLS LAST\n"
        ") best\n"
        "ORDER BY rank, title\n"
        "LIMIT :lim"
    )
    rows = query("law_db", sql, params)
    if isinstance(rows, dict):
        return rows
    rows = _dedupe_authoritative(rows)
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
