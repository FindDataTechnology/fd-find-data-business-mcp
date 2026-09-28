"""World Bank tools — federated, read-only access to the world_bank store.

``wb_search_indicators`` discovers WDI indicators by fuzzy name or topic;
``wb_read`` serves one indicator's annual series for a list of countries and
a year range (country given as ISO3 code or Chinese name). Both go through
the fail-soft domain layer and brand every served row ``finddata``; no
table or provenance identity is emitted beyond the codes the consumer
supplied. Search rows additionally carry the unified indicator-registry
identity (``native_code`` + ``semantic_code``), enriched fail-soft.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from fd_find_data_business_mcp.domain import (
    query as domain_query, registry_semantic_codes)

_DB = "world_bank"

#: Registry anchor for this store: native_code is the WDI indicator code.
_REGISTRY_ANCHOR = ("world_bank", "indicator", "code")

_SEARCH_LIMIT_MAX = 200
_READ_LIMIT_MAX = 5000  # 66y x N countries; bounded and truncation-marked

_SEARCH_SQL = """
SELECT ind.code AS code, ind.name_cn AS name, ind.name_en AS name_en,
       ind.topic AS topic, ind.unit AS unit
FROM indicator ind
WHERE ind.name_cn ILIKE :pat
   OR ind.name_en ILIKE :pat
   OR ind.topic ILIKE :pat
   OR ind.code ILIKE :pat
ORDER BY (lower(ind.code) = lower(:q)) DESC,
         (lower(ind.name_cn) = lower(:q)) DESC,
         (ind.name_cn ILIKE :pfx) DESC,
         position(lower(:q) IN lower(ind.name_cn)),
         ind.code
LIMIT :lim
"""

_COUNTRY_SQL = """
SELECT code, name_cn
FROM country
WHERE code = ANY(:raw) OR name_cn = ANY(:raw)
"""

_READ_SQL = """
SELECT o.year AS year, o.country_code AS country_code, o.value AS value,
       c.name_cn AS country_name, ind.unit AS unit
FROM observation o
LEFT JOIN country c ON c.code = o.country_code
LEFT JOIN indicator ind ON ind.code = o.indicator_code
WHERE o.indicator_code = :icode
  AND o.country_code = ANY(:ccodes)
  AND o.year BETWEEN :sy AND :ey
ORDER BY o.country_code, o.year
LIMIT :lim
"""


def _escape_like(s: str) -> str:
    """Neutralize LIKE wildcards in user input (backslash is the default escape)."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _clamp(limit: int, max_limit: int) -> int:
    return max(1, min(limit, max_limit))


def _num(v: Any) -> Any:
    """Serve numerics as JSON-safe floats; anything else passes through."""
    if isinstance(v, Decimal):
        return float(v)
    return v


def wb_search_indicators(query_text: str, limit: int = 50) -> dict:
    """Search World Bank (WDI) indicators by fuzzy name, topic, or code.

    Matches Chinese and English names, topics, and codes with
    ``ILIKE '%q%'``, ranking exact code/name matches first. Returns
    ``{results, count, truncated}`` with rows
    ``{code, name, name_en, topic, unit, native_code, semantic_code,
    brand}``: ``native_code`` repeats the registry anchor (the WDI code)
    and ``semantic_code`` carries the unified indicator-registry identity —
    null when the entry is unverified or unregistered (the lookup fails
    soft). A domain failure is passed through unchanged.
    """
    q = (query_text or "").strip()
    if not q:
        return {"results": [], "count": 0, "truncated": False}
    limit = _clamp(limit, _SEARCH_LIMIT_MAX)
    esc = _escape_like(q)
    rows = domain_query(_DB, _SEARCH_SQL, {
        "pat": f"%{esc}%", "pfx": f"{esc}%", "q": q, "lim": limit + 1,
    })
    if isinstance(rows, dict):
        return rows
    page = rows[:limit]
    native_codes = [r["code"] for r in page]
    semantic = registry_semantic_codes(*_REGISTRY_ANCHOR, native_codes)
    results = [
        {"code": r["code"], "name": r["name"], "name_en": r["name_en"],
         "topic": r["topic"], "unit": r["unit"], "native_code": r["code"],
         "semantic_code": semantic.get(r["code"]), "brand": "finddata"}
        for r in page
    ]
    return {"results": results, "count": len(results), "truncated": len(rows) > limit}


def wb_read(
    indicator_code: str,
    countries: list[str],
    start_year: int | None = None,
    end_year: int | None = None,
    limit: int = 600,
) -> dict:
    """Read one indicator's annual series for a list of countries.

    ``countries`` accepts ISO3 codes (``CHN``) or Chinese names (``中国``);
    inputs that resolve to no country are simply absent from the result —
    never fabricated. Returns ``{results, count, truncated}`` with rows
    ``{year, country, country_name, value, unit, brand}`` ordered by
    country then year; an indicator or filter with no data is a truthful
    empty envelope, and a domain failure is passed through unchanged.
    """
    code = (indicator_code or "").strip()
    raw = [c.strip() for c in (countries or []) if c and c.strip()]
    if not code or not raw:
        return {"results": [], "count": 0, "truncated": False}
    limit = _clamp(limit, _READ_LIMIT_MAX)

    resolved = domain_query(_DB, _COUNTRY_SQL, {"raw": raw})
    if isinstance(resolved, dict):
        return resolved
    if not resolved:
        return {"results": [], "count": 0, "truncated": False}
    ccodes = sorted({r["code"] for r in resolved})

    params: dict[str, Any] = {
        "icode": code, "ccodes": ccodes, "lim": limit + 1,
        "sy": start_year if start_year is not None else -2147483648,
        "ey": end_year if end_year is not None else 2147483647,
    }
    rows = domain_query(_DB, _READ_SQL, params)
    if isinstance(rows, dict):
        return rows
    truncated = len(rows) > limit
    results = [
        {"year": r["year"], "country": r["country_code"],
         "country_name": r["country_name"],
         "value": _num(r["value"]), "unit": r["unit"], "brand": "finddata"}
        for r in rows[:limit]
    ]
    return {"results": results, "count": len(results), "truncated": truncated}
