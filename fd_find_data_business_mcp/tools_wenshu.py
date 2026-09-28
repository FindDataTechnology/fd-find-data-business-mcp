"""Wenshu (裁判文书) domain tools over the federated wenshu_db (finddata-branded).

wenshu_db.judgments (doc_id text PK) is a stock SNAPSHOT of China's
judgment-document corpus: incremental crawling is PAUSED and the coverage
window has known gaps (including stretches of 2011-2023) — it is NOT the
full corpus, and corpus-level answers must say so. Metadata lives in PG;
bodies live out-of-band in RustFS zstd blocks (see wenshu_body) behind the
internal ``block_key`` coordinate.

Decode-JOIN verification (live wenshu_db, 2026-09-28, TABLESAMPLE 0.3% ≈
32.8k rows): every non-NULL ``*引用`` int resolves through
``dict_texts("列", ref_key, txt)`` at 100.00% for all ten columns (法院/
案由/省/市/案件类型/文书类型/审理程序/案例级别/法院级别/法条 — 法条 stores
the whole multi-article string 1:1 with its ref_key, no one-to-many
residue in practice). Refs are NULL exactly where the crawl row lacked
the value, so decodes are LEFT JOINs and missing fields surface as null
honestly — never guessed, never joined away.

Crawler/coordinates provenance (id_origin, block_key, block_ordinal,
src_id) and the raw int ``*引用`` refs never leave this module: search
SQL never selects them and the row projection whitelist drops them even
if a row carried them. Every payload carries ``brand: "finddata"``.
"""
from __future__ import annotations

from typing import Any

from fd_find_data_business_mcp import wenshu_body
from fd_find_data_business_mcp.domain import query

_BRAND = "finddata"

#: Internal coordinates and raw dictionary refs that must never reach a
#: customer payload. Search SQL never selects them; read SQL needs only
#: ``block_key`` (to locate the body block) and this filter keeps it in.
_FORBIDDEN_KEYS = frozenset({
    "id_origin", "block_key", "block_ordinal", "src_id",
    "法院引用", "案由引用", "省引用", "市引用", "案件类型引用",
    "文书类型引用", "审理程序引用", "案例级别引用", "法院级别引用", "法条引用",
})

_MAX_LIMIT = 200

#: Whitelist: search hits carry only these row fields — decoded dictionary
#: text, never the raw refs, never the block coordinates.
_SEARCH_FIELDS = (
    "doc_id", "案号", "标题", "法院", "案由", "省", "市", "案件类型",
    "文书类型", "审理程序", "法院级别", "判决年份", "判决结果", "发布时间",
)

#: Reads add the two remaining dates and the raw 正文形态 as ``body_shape``.
_READ_FIELDS = _SEARCH_FIELDS + ("判决时间", "受理时间", "body_shape")

_NO_BODY_NOTE = "该篇文书仅元数据入库（正文形态为 none，无正文块），现仅提供元数据。"

_BODY_UNAVAILABLE_NOTE = (
    "正文块暂时不可读（对象存储访问失败），元数据仍完整，请稍后重试。"
)

#: Decode JOINs shared by search and read (alias -> dict 列).
_DECODE_JOINS = (
    ('df', "法院", "法院引用"),
    ('da', "案由", "案由引用"),
    ('dp', "省", "省引用"),
    ('dc', "市", "市引用"),
    ('dct', "案件类型", "案件类型引用"),
    ('dwt', "文书类型", "文书类型引用"),
    ('dpr', "审理程序", "审理程序引用"),
    ('dcl', "法院级别", "法院级别引用"),
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


def _decode_join_sql() -> str:
    return "\n".join(
        f'LEFT JOIN dict_texts {alias} ON {alias}."列" = \'{col}\''
        f' AND {alias}.ref_key = j."{ref}"'
        for alias, col, ref in _DECODE_JOINS
    )


def _result(row: dict[str, Any]) -> dict[str, Any]:
    """Project one search hit onto the public metadata whitelist."""
    out = {k: row.get(k) for k in _SEARCH_FIELDS}
    out["has_body"] = bool(row.get("has_body"))
    out["brand"] = _BRAND
    return {k: v for k, v in out.items() if k not in _FORBIDDEN_KEYS}


def wenshu_search(
    title_query: str, doc_type: str | None = None,
    province: str | None = None, start_year: int | None = None,
    end_year: int | None = None, limit: int = 50,
) -> dict[str, Any]:
    """Search judgment titles, ranked exact > prefix > contains.

    Case-insensitive substring match on 标题 (案号 is embedded there too,
    so "（2024）京01民初1号"-style queries work without a separate
    parameter). Optional filters: ``doc_type`` (decoded 文书类型 equality,
    e.g. 判决书/裁定书/调解书), ``province`` (decoded 省 equality),
    ``start_year``/``end_year`` (closed interval on 判决年份). At most 200
    rows; empty or blank queries match nothing without hitting PG.

    Corpus window disclosure: 存量快照：增量爬取已暂停，覆盖窗口存在已知
    缺口（含 2011–2023 段），并非全量语料 — an empty or thin result says
    the corpus missed it, not that the document does not exist.

    Operational envelope: over this 10M+-row table the rank order must see
    every ILIKE match, so a very broad pattern (hundreds of thousands of
    hits, e.g. a bare 合同) can exceed the 15s federated statement timeout
    and surface as ``{"status": "domain_unavailable"}``; narrowing with
    ``start_year``/``end_year`` (btree, index-ANDed with the trgm title
    index), ``doc_type`` or ``province``, or searching a longer phrase
    keeps it well inside budget.

    Returns ``{results: [metadata + brand], count, truncated, brand}`` —
    bodies are never included; ``has_body`` says whether
    :func:`wenshu_read` can serve one. 发布时间 is ``YYYY-MM-DD`` text.
    A federation failure passes through as
    ``{"status": "domain_unavailable"}``.
    """
    q = (title_query or "").strip()
    if not q:
        return {"results": [], "count": 0, "truncated": False, "brand": _BRAND}
    limit = min(max(int(limit), 1), _MAX_LIMIT)
    esc = _escape_like(q)
    params: dict[str, Any] = {
        "pattern": f"%{esc}%",
        "exact": esc,
        "prefix": f"{esc}%",
        "lim": limit + 1,
    }
    sql = (
        "SELECT j.\"doc_id\" AS doc_id, j.\"案号\" AS 案号, j.\"标题\" AS 标题,\n"
        "       df.txt AS 法院, da.txt AS 案由, dp.txt AS 省, dc.txt AS 市,\n"
        "       dct.txt AS 案件类型, dwt.txt AS 文书类型, dpr.txt AS 审理程序,\n"
        "       dcl.txt AS 法院级别,\n"
        "       j.\"判决年份\" AS 判决年份, j.\"判决结果\" AS 判决结果,\n"
        "       j.\"发布时间\"::date::text AS 发布时间,\n"
        "       (COALESCE(j.\"正文形态\", 'none') <> 'none'"
        " AND j.\"block_key\" IS NOT NULL) AS has_body,\n"
        "       CASE WHEN j.\"标题\" ILIKE :exact THEN 0\n"
        "            WHEN j.\"标题\" ILIKE :prefix THEN 1\n"
        "            ELSE 2 END AS rank\n"
        "FROM judgments j\n"
        + _decode_join_sql() +
        "\nWHERE j.\"标题\" ILIKE :pattern\n"
    )
    dtype = (doc_type or "").strip() or None
    if dtype is not None:
        params["doc_type"] = dtype
        sql += "  AND dwt.txt = :doc_type\n"
    prov = (province or "").strip() or None
    if prov is not None:
        params["province"] = prov
        sql += "  AND dp.txt = :province\n"
    if start_year is not None:
        params["start_year"] = int(start_year)
        sql += "  AND j.\"判决年份\" >= :start_year\n"
    if end_year is not None:
        params["end_year"] = int(end_year)
        sql += "  AND j.\"判决年份\" <= :end_year\n"
    sql += "ORDER BY rank, 标题\nLIMIT :lim"
    rows = query("wenshu_db", sql, params)
    if isinstance(rows, dict):
        return rows
    needle = q.lower()
    rows = sorted(rows, key=lambda r: (_rank(r.get("标题"), needle), r.get("标题") or ""))
    truncated = len(rows) > limit
    return {
        "results": [_result(r) for r in rows[:limit]],
        "count": min(len(rows), limit),
        "truncated": truncated,
        "brand": _BRAND,
    }


def wenshu_read(doc_id: str) -> dict[str, Any]:
    """Read one judgment by doc_id: full metadata plus the body when servable.

    Metadata mirrors the search whitelist plus 判决时间/受理时间 (as
    ``YYYY-MM-DD`` text) and ``body_shape`` (raw 正文形态). Body
    resolution is honest in all three states:

    - ``正文形态 = 'none'`` or no ``block_key`` → ``content_available:
      false`` with a note that only metadata was ever ingested;
    - block fetch fails (object store unreachable/corrupt) →
      ``content_available: false`` with a retry note — metadata stays
      complete;
    - block fetched → ``content_available: true`` and the full ``content``.

    Returns ``{"status": "not_found"}`` for an unknown doc_id; a
    federation failure passes through as
    ``{"status": "domain_unavailable"}``. The internal ``block_key``
    coordinate never appears in the response.
    """
    did = (doc_id or "").strip()
    if not did:
        return {"status": "not_found", "brand": _BRAND}
    rows = query(
        "wenshu_db",
        "SELECT j.\"doc_id\" AS doc_id, j.\"案号\" AS 案号, j.\"标题\" AS 标题,\n"
        "       df.txt AS 法院, da.txt AS 案由, dp.txt AS 省, dc.txt AS 市,\n"
        "       dct.txt AS 案件类型, dwt.txt AS 文书类型, dpr.txt AS 审理程序,\n"
        "       dcl.txt AS 法院级别,\n"
        "       j.\"判决年份\" AS 判决年份, j.\"判决结果\" AS 判决结果,\n"
        "       j.\"判决时间\"::date::text AS 判决时间,\n"
        "       j.\"发布时间\"::date::text AS 发布时间,\n"
        "       j.\"受理时间\"::date::text AS 受理时间,\n"
        "       j.\"正文形态\" AS body_shape, j.\"block_key\"\n"
        "FROM judgments j\n" + _decode_join_sql() +
        "\nWHERE j.\"doc_id\" = :doc_id",
        {"doc_id": did},
    )
    if isinstance(rows, dict):
        return rows
    if not rows:
        return {"status": "not_found", "brand": _BRAND}
    row = rows[0]
    out: dict[str, Any] = {k: row.get(k) for k in _READ_FIELDS}
    out["brand"] = _BRAND
    block_key = row.get("block_key")
    if (row.get("body_shape") or "none") == "none" or not block_key:
        out["content_available"] = False
        out["note"] = _NO_BODY_NOTE
    else:
        content = wenshu_body.fetch_judgment_body(block_key, did)
        if content is None:
            out["content_available"] = False
            out["note"] = _BODY_UNAVAILABLE_NOTE
        else:
            out["content_available"] = True
            out["content"] = content
    return {k: v for k, v in out.items() if k not in _FORBIDDEN_KEYS}
