"""Guide-cases tools over the federated law_db (finddata-branded).

Title search plus full-content single reads over ``law_db.guide_cases``
(最高法指导性案例 + 发布通知). Ranked exact > prefix > contains like the
law tools; doc_type and reference_status are closed-vocabulary filters —
anything outside the vocabulary is refused, never guessed. Provenance
columns (content_hash/fetched_at) never leave this module; every payload
carries ``brand: "finddata"``. reference_status stays visible in search
results so consumers can see 参照效力 directly; "不再参照" entries remain
readable and searchable — the notice withdraws the precedent effect, not
the document itself.
"""
from __future__ import annotations

from typing import Any

from fd_find_data_business_mcp.domain import query

_BRAND = "finddata"

#: Crawler bookkeeping columns — provenance isolation: never in a response.
_FORBIDDEN_KEYS = frozenset({"content_hash", "fetched_at"})

_MAX_LIMIT = 200

_DOC_TYPES = frozenset({"case", "notice"})

_REFERENCE_STATUSES = frozenset({"有效", "不再参照"})

#: The six published sub-sections of a case body. A section that was never
#: published is NULL in the corpus and its key is omitted entirely from a
#: read response (no null placeholders).
_SECTION_KEYS = ("keyword", "gist", "facts", "result", "reasoning", "statutes")

_SEARCH_FIELDS = (
    "source_id", "case_no", "doc_type", "title",
    "listed_at", "publish_at", "source", "reference_status",
)

_READ_FIELDS = (
    "source_id", "case_no", "doc_type", "title",
    "listed_at", "publish_at", "source", "source_url",
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


def _bad_filter(field: str, valid: frozenset[str]) -> dict[str, Any]:
    return {
        "status": "invalid_filter",
        "field": field,
        "valid_values": sorted(valid),
        "brand": _BRAND,
    }


def _result(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: row.get(k) for k in _SEARCH_FIELDS}
    out["brand"] = _BRAND
    return {k: v for k, v in out.items() if k not in _FORBIDDEN_KEYS}


def guide_cases_search(
    title_query: str,
    doc_type: str | None = None,
    reference_status: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Search FindData guiding-case titles, ranked exact > prefix > contains.

    Case-insensitive substring match on the title, optional closed-vocabulary
    filters (doc_type: case/notice; reference_status: 有效/不再参照 — any
    other value is refused as ``invalid_filter``), at most 200 rows. Each
    result row carries reference_status so 参照效力 is visible without a
    follow-up read. Returns ``{results: [metadata + brand], count,
    truncated}`` — case bodies are never included in search results.
    """
    q = (title_query or "").strip()
    if not q:
        return {"results": [], "count": 0, "truncated": False}
    if doc_type:
        doc_type = str(doc_type).strip()
        if doc_type not in _DOC_TYPES:
            return _bad_filter("doc_type", _DOC_TYPES)
    if reference_status:
        reference_status = str(reference_status).strip()
        if reference_status not in _REFERENCE_STATUSES:
            return _bad_filter("reference_status", _REFERENCE_STATUSES)
    limit = min(max(limit, 1), _MAX_LIMIT)
    esc = _escape_like(q)
    params: dict[str, Any] = {
        "pattern": f"%{esc}%",
        "exact": esc,
        "prefix": f"{esc}%",
        "lim": limit + 1,
    }
    sql = (
        "SELECT source_id, case_no, doc_type, title,\n"
        "       listed_at::date::text AS listed_at,\n"
        "       publish_at::date::text AS publish_at,\n"
        "       source, reference_status,\n"
        "       CASE WHEN title ILIKE :exact THEN 0\n"
        "            WHEN title ILIKE :prefix THEN 1\n"
        "            ELSE 2 END AS rank\n"
        "FROM guide_cases\n"
        "WHERE title ILIKE :pattern\n"
    )
    if doc_type:
        params["doc_type"] = doc_type
        sql += "  AND doc_type = :doc_type\n"
    if reference_status:
        params["reference_status"] = reference_status
        sql += "  AND reference_status = :reference_status\n"
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
    }


def guide_cases_read(case_id: str) -> dict[str, Any]:
    """Read one FindData guiding case (or issue notice) by source_id.

    Returns full metadata (incl. source_url), the six sub-sections — a
    section that was never published is omitted entirely, never a null
    placeholder — the always-present ``body`` full text, and the
    reference_status/reference_note pair (which document withdrew the
    参照效力). An unknown id returns ``{"status": "not_found"}``; a federation
    failure passes through as ``{"status": "domain_unavailable"}``.
    """
    rows = query(
        "law_db",
        "SELECT source_id, case_no, doc_type, title,\n"
        "       listed_at::date::text AS listed_at,\n"
        "       publish_at::date::text AS publish_at,\n"
        "       source, source_url,\n"
        "       keyword, gist, facts, result, reasoning, statutes,\n"
        "       body, reference_status, reference_note\n"
        "FROM guide_cases\n"
        "WHERE source_id = :case_id",
        {"case_id": case_id},
    )
    if isinstance(rows, dict):
        return rows
    if not rows:
        return {"status": "not_found", "brand": _BRAND}
    row = rows[0]
    out: dict[str, Any] = {k: row.get(k) for k in _READ_FIELDS}
    for key in _SECTION_KEYS:
        val = row.get(key)
        if val:
            out[key] = val
    out["body"] = row.get("body")
    out["reference_status"] = row.get("reference_status")
    out["reference_note"] = row.get("reference_note")
    out["brand"] = _BRAND
    return {k: v for k, v in out.items() if k not in _FORBIDDEN_KEYS}
