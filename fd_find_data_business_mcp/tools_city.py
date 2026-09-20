"""City panel tools — federated, read-only access to china_city_panel.

``city_search_variables`` discovers city-panel variables by fuzzy Chinese
name; ``city_read`` serves one variable's city-year values, optionally
filtered by one city (code or Chinese name) and a year range. The wide
panel table (panel_raw, v001..v209) is only ever queried with an
allowlisted variable code. Both tools go through the fail-soft domain layer
and brand every served row ``finddata``.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from fd_find_data_business_mcp.domain import query as domain_query

_DB = "china_city_panel"

_VAR_ID_RE = re.compile(r"^v\d{3}$")

_SEARCH_LIMIT_MAX = 200
_READ_LIMIT_MAX = 10000  # full panel = 297 cities x 25 years = 7425 rows

_SEARCH_SQL = """
SELECT c.var_id AS code, c.name_cn AS label, c.unit AS unit
FROM variable_catalog c
WHERE c.name_cn ILIKE :pat
   OR c.var_id ILIKE :pat
ORDER BY (lower(c.var_id) = lower(:q)) DESC,
         (c.name_cn ILIKE :pfx) DESC,
         position(lower(:q) IN lower(c.name_cn)),
         c.var_id
LIMIT :lim
"""

_VAR_SQL = """
SELECT var_id
FROM variable_catalog
WHERE var_id = :var_id
"""

_CITY_SQL = """
SELECT city_code, city_name
FROM city_dim
WHERE city_code = :ccode OR city_name = :cname
"""

_READ_SQL = """
SELECT p.year AS year, p.city_code AS city_code, d.city_name AS city_name,
       p.{col} AS value
FROM panel_raw p
LEFT JOIN city_dim d ON d.city_code = p.city_code
WHERE p.year BETWEEN :sy AND :ey
  AND p.{col} IS NOT NULL
  {city_clause}
ORDER BY p.year, p.city_code
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


def city_search_variables(query_text: str, limit: int = 50) -> dict:
    """Search city-panel variables by fuzzy Chinese name or code.

    Returns ``{results, count, truncated}`` with rows
    ``{code, label, unit, brand}``; a domain failure is passed through
    unchanged.
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
        {"code": r["code"], "label": r["label"], "unit": r["unit"],
         "brand": "finddata"}
        for r in rows[:limit]
    ]
    return {"results": results, "count": len(results), "truncated": len(rows) > limit}


def city_read(
    variable: str,
    city: str | None = None,
    start_year: int | None = None,
    end_year: int | None = None,
    limit: int = 2000,
) -> dict:
    """Read one city-panel variable's city-year values.

    ``city`` accepts a city code or Chinese name; a name that resolves to
    no city yields a truthful empty envelope. Returns ``{results, count,
    truncated}`` with rows ``{year, city_code, city_name, value, brand}``
    ordered by year then city; an unknown variable returns
    ``{"status": "unknown_variable"}``; a filter with no data is a truthful
    empty envelope; a domain failure is passed through unchanged.
    """
    vid = (variable or "").strip().lower()
    if not _VAR_ID_RE.match(vid):
        return {"status": "unknown_variable", "variable": variable}
    limit = _clamp(limit, _READ_LIMIT_MAX)

    known = domain_query(_DB, _VAR_SQL, {"var_id": vid})
    if isinstance(known, dict):
        return known
    if not known:
        return {"status": "unknown_variable", "variable": vid}

    city_clause = ""
    params: dict[str, Any] = {
        "sy": start_year if start_year is not None else -2147483648,
        "ey": end_year if end_year is not None else 2147483647,
        "lim": limit + 1,
    }
    if city is not None:
        # city_code is integer and city_name is text, so the two comparisons
        # need separate parameters — one text-valued parameter compared to
        # both columns makes PostgreSQL fail to resolve its type.
        c = city.strip()
        resolved = domain_query(_DB, _CITY_SQL, {
            "ccode": int(c) if c.isdigit() else -1, "cname": c})
        if isinstance(resolved, dict):
            return resolved
        if not resolved:
            return {"results": [], "count": 0, "truncated": False}
        city_clause = "AND p.city_code = :ccode"
        params["ccode"] = resolved[0]["city_code"]

    sql = _READ_SQL.format(col=vid, city_clause=city_clause)
    rows = domain_query(_DB, sql, params)
    if isinstance(rows, dict):
        return rows
    truncated = len(rows) > limit
    results = [
        {"year": r["year"], "city_code": r["city_code"],
         "city_name": r["city_name"], "value": _num(r["value"]),
         "brand": "finddata"}
        for r in rows[:limit]
    ]
    return {"results": results, "count": len(results), "truncated": truncated}
