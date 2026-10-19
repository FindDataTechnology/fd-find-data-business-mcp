"""Legal-documents domain tools over the federated law_db (finddata-branded).

public.legal_documents (~30k rows) unifies five crawled Chinese legal or
regulatory corpora (纪检监察法规 / 政府规章 / 外交部条约 / 党内法规 / 人民法院
案例) behind one customer-facing ``source`` vocabulary. Metadata-only
ranked title search (exact > prefix > contains) plus single-document
reads; rows whose body was never fetched (async-rendered pages or
oversized attachments — most of the CCDI corpus) answer honestly with
``content_available: false`` and no ``content`` key. Crawler provenance
(source_id / content_hash / fetched_at / loaded_at) never leaves this
module; every constructed payload carries ``brand: "finddata"``.
"""
from __future__ import annotations

from typing import Any

from fd_find_data_business_mcp import limits
from fd_find_data_business_mcp.domain import query

_BRAND = "finddata"

#: Crawler provenance that must never reach a customer payload. Deliberately
#: NOT tools_law's blacklist: this table's ``source`` column IS the public
#: five-corpus vocabulary and is returned as-is.
_FORBIDDEN_KEYS = frozenset({
    "source_id", "content_hash", "fetched_at", "loaded_at",
})

# 上限的唯一真源在 limits.py（mcp-result-contract）
_MAX_LIMIT = limits.MAX_LIST_ITEMS

#: The five crawled corpora, exactly as stored in legal_documents.source.
#: Anything else is rejected client-side instead of silently matching nothing.
_SOURCES: tuple[str, ...] = (
    "ccdi-supervisory-regulations",
    "gov-rules-library",
    "mfa-treaty-library",
    "party-regulation-library",
    "rmfyalk-case-library",
)

_SOURCE_LABELS_ZH: dict[str, str] = {
    "ccdi-supervisory-regulations": "纪检监察法规库",
    "gov-rules-library": "政府规章库",
    "mfa-treaty-library": "外交部条约库",
    "party-regulation-library": "党内法规库",
    "rmfyalk-case-library": "人民法院案例库",
}

#: Whitelist: search hits carry only these row fields — never the body, never
#: the provenance columns, never the crawler metadata jsonb.
_SEARCH_FIELDS = (
    "id", "title", "source", "doc_type", "status", "authority",
    "language", "published_at", "effective_at", "has_content",
)

#: Reads additionally expose the public provenance link and the crawler
#: metadata jsonb (minus its internal ``object_keys`` entry).
_READ_FIELDS = (
    "id", "title", "source", "doc_type", "status", "authority",
    "language", "published_at", "effective_at", "source_url", "metadata",
)

_NO_BODY_NOTE = (
    "该文档正文未抓取入库：源站正文为页面异步渲染或附件体积超限所致，"
    "现仅提供元数据。"
)


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


def _result(row: dict[str, Any]) -> dict[str, Any]:
    """Project one search hit onto the public metadata whitelist."""
    out = {k: row.get(k) for k in _SEARCH_FIELDS}
    out["source_label_zh"] = _SOURCE_LABELS_ZH.get(row.get("source") or "")
    out["has_content"] = bool(row.get("has_content"))
    out["brand"] = _BRAND
    return {k: v for k, v in out.items() if k not in _FORBIDDEN_KEYS}


def _strip_object_keys(metadata: Any) -> Any:
    """Drop the crawler-internal ``object_keys`` entry from a jsonb payload."""
    if isinstance(metadata, dict):
        return {k: v for k, v in metadata.items() if k != "object_keys"}
    return metadata


def legal_docs_search(
    title_query: str, source: str | None = None,
    doc_type: str | None = None, limit: int = 50,
) -> dict[str, Any]:
    """Search legal-document titles, ranked exact > prefix > contains.

    Case-insensitive substring match on the title; optional ``source``
    filter (one of the five corpus codes — anything else is rejected as
    ``{"status": "invalid_filter", "field": "source", "valid_sources":
    [...]}``); optional ``doc_type`` equality filter; at most 200 rows.
    Returns ``{results: [metadata + brand], count, truncated, brand}`` —
    document bodies are never included; ``has_content`` says whether
    :func:`legal_docs_read` can serve one. Timestamps are served as
    ``YYYY-MM-DD`` text. Empty or blank queries match nothing.
    """
    q = (title_query or "").strip()
    if not q:
        return {"results": [], "count": 0, "truncated": False, "brand": _BRAND}
    src = (source or "").strip() or None
    if src is not None and src not in _SOURCES:
        return {
            "status": "invalid_filter",
            "field": "source",
            "valid_sources": list(_SOURCES),
            "brand": _BRAND,
        }
    limit = min(max(limit, 1), _MAX_LIMIT)
    esc = _escape_like(q)
    params: dict[str, Any] = {
        "pattern": f"%{esc}%",
        "exact": esc,
        "prefix": f"{esc}%",
        "lim": limit + 1,
    }
    sql = (
        "SELECT id, title, source, doc_type, status, authority, language,\n"
        "       published_at::date::text AS published_at,\n"
        "       effective_at::date::text AS effective_at,\n"
        "       (content IS NOT NULL) AS has_content,\n"
        "       CASE WHEN title ILIKE :exact THEN 0\n"
        "            WHEN title ILIKE :prefix THEN 1\n"
        "            ELSE 2 END AS rank\n"
        "FROM legal_documents\n"
        "WHERE title ILIKE :pattern\n"
    )
    if src is not None:
        params["source"] = src
        sql += "  AND source = :source\n"
    dtype = (doc_type or "").strip() or None
    if dtype is not None:
        params["doc_type"] = dtype
        sql += "  AND doc_type = :doc_type\n"
    sql += "ORDER BY rank, title\nLIMIT :lim"
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
        "brand": _BRAND,
    }


def legal_docs_read(
    doc_id: int,
    offset: int = 0,
    max_chars: int = limits.DEFAULT_BODY_CHARS,
) -> dict[str, Any]:
    """Read one legal document by id, body included only when it was fetched.

    Full metadata (including ``source_url`` and the crawler ``metadata``
    jsonb minus its internal ``object_keys`` entry) plus
    ``content_available``. When the body never made it into the corpus the
    answer carries an honest ``note`` and the ``content`` key is absent —
    not an empty string. Returns ``{"status": "not_found"}`` for an
    unknown id; a federation failure passes through as
    ``{"status": "domain_unavailable"}``.

    Bodies are windowed: at most ``max_chars`` characters (default 12000,
    cap 40000) from ``offset``. The corpus holds documents well past the
    default (longest observed: 177,139 characters), and an unwindowed read
    would consume the caller's whole context in one call. ``total_chars``
    and ``next_offset`` give the continuation point; slices are taken on
    character boundaries, so concatenating pages reproduces the original
    text exactly.
    """
    rows = query(
        "law_db",
        "SELECT id, title, source, doc_type, status, authority, language,\n"
        "       published_at::date::text AS published_at,\n"
        "       effective_at::date::text AS effective_at,\n"
        "       source_url, metadata,\n"
        "       (content IS NOT NULL) AS content_available, content\n"
        "FROM legal_documents\n"
        "WHERE id = :doc_id",
        {"doc_id": doc_id},
    )
    if isinstance(rows, dict):
        return rows
    if not rows:
        return {"status": "not_found", "brand": _BRAND}
    row = rows[0]
    out: dict[str, Any] = {k: row.get(k) for k in _READ_FIELDS}
    out["source_label_zh"] = _SOURCE_LABELS_ZH.get(row.get("source") or "")
    out["metadata"] = _strip_object_keys(out.get("metadata"))
    out["content_available"] = bool(row.get("content_available"))
    if out["content_available"]:
        body = row.get("content") or ""
        start_at = max(0, int(offset))
        if start_at >= len(body) and body:
            return {"status": "not_found", "brand": _BRAND,
                    "note": f"offset {start_at} 超出正文长度 {len(body)}；"
                            "用 total_chars 内的偏移重试。"}
        chunk, _end, _more = limits.slice_window(body, start_at, max_chars)
        out["content"] = chunk
        out.update(limits.truncation_fields(
            total=len(body), offset=start_at, returned=len(chunk)))
    else:
        out["note"] = _NO_BODY_NOTE
    out["brand"] = _BRAND
    return {k: v for k, v in out.items() if k not in _FORBIDDEN_KEYS}
