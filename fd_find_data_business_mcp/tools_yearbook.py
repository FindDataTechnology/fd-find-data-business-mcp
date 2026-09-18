"""Yearbook tools — federated, read-only access to the yearbook catalog.

``yearbook_search_indicators`` discovers indicators by fuzzy name over the
standardized catalog (indicators + aliases); ``yearbook_read`` serves the
latest-edition series for one indicator from indicator_values. Both go
through the fail-soft domain layer and brand every served row ``finddata``;
no table_id, dims, or provenance key is ever emitted.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from fd_find_data_business_mcp.domain import query as domain_query

_DB = "yearbook_catalog"

_SEARCH_LIMIT_MAX = 200
_READ_LIMIT_MAX = 1000

_SEARCH_SQL = """
SELECT ind.id AS id, ind.std_name AS name, ind.category AS category,
       ind.std_unit AS unit
FROM indicators ind
WHERE ind.std_name ILIKE :pat
   OR ind.id IN (SELECT a.indicator_id FROM indicator_aliases a
                 WHERE a.indicator_id IS NOT NULL AND a.alias ILIKE :pat)
ORDER BY (lower(ind.std_name) = lower(:q)) DESC,
         (ind.std_name ILIKE :pfx) DESC,
         position(lower(:q) IN lower(ind.std_name)),
         ind.std_name
LIMIT :lim
"""

_READ_SELECT = """
SELECT DISTINCT ON (region, data_year)
       data_year, region, value_num, value, unit
FROM indicator_values
WHERE {conds}
ORDER BY region, data_year, edition_year DESC NULLS LAST
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


def yearbook_search_indicators(query_text: str, limit: int = 50) -> dict:
    """Search yearbook indicators by fuzzy name.

    Matches standardized names and recorded aliases with ``ILIKE '%q%'``,
    ranking exact matches first, then prefix matches, then earliest
    substring position. Returns ``{results, count, truncated}`` with rows
    ``{id, name, category, unit, brand}``; a domain failure is passed
    through unchanged.
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
    results = [
        {"id": r["id"], "name": r["name"], "category": r["category"],
         "unit": r["unit"], "brand": "finddata"}
        for r in rows[:limit]
    ]
    return {"results": results, "count": len(results), "truncated": len(rows) > limit}


def yearbook_read(
    indicator_id: int,
    region: str | None = None,
    start_year: int | None = None,
    end_year: int | None = None,
    limit: int = 200,
) -> dict:
    """Read the value series of one yearbook indicator.

    Serves one row per (region, data_year) — the latest ``edition_year``
    only, chosen in SQL with DISTINCT ON — ordered by year. ``value`` is
    the numeric form when available, else the raw string. Returns
    ``{results, count, truncated}``; no rows is a truthful empty envelope,
    and a domain failure is passed through unchanged.
    """
    limit = _clamp(limit, _READ_LIMIT_MAX)
    conds = ["indicator_id = :iid"]
    params: dict[str, Any] = {"iid": indicator_id, "lim": limit + 1}
    if region is not None:
        conds.append("region = :region")
        params["region"] = region
    if start_year is not None or end_year is not None:
        conds.append("data_year BETWEEN :sy AND :ey")
        params["sy"] = start_year if start_year is not None else -2147483648
        params["ey"] = end_year if end_year is not None else 2147483647
    rows = domain_query(_DB, _READ_SELECT.format(conds=" AND ".join(conds)), params)
    if isinstance(rows, dict):
        return rows
    truncated = len(rows) > limit
    results = []
    for r in rows[:limit]:
        value = r["value_num"] if r["value_num"] is not None else r["value"]
        results.append({"year": r["data_year"], "region": r["region"],
                        "value": _num(value), "unit": r["unit"], "brand": "finddata"})
    results.sort(key=lambda row: row["year"])
    return {"results": results, "count": len(results), "truncated": truncated}
