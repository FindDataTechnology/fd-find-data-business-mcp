"""GTA panel tools — federated, read-only access to the gta_panel store.

``gta_search_variables`` discovers panel variables by fuzzy Chinese label or
code; ``gta_read`` serves one variable's firm-year values, optionally
filtered by year range and firm identifiers. The wide panel tables
(panel_c0/c1/c2, up to 849 columns) are only ever queried with explicit,
allowlisted column names. Both tools go through the fail-soft domain layer
and brand every served row ``finddata``; internal storage identity (table
and sub-library names) is never emitted.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from fd_find_data_business_mcp.domain import query as domain_query

_DB = "gta_panel"

#: Variable storage is only servable from these tables (validated before any
#: column name is interpolated into SQL).
_TABLES = ("panel_c0", "panel_c1", "panel_c2")
_VAR_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")

_SEARCH_LIMIT_MAX = 200
_READ_LIMIT_MAX = 2000

_SEARCH_SQL = """
SELECT v.name AS code, v.label_cn AS label
FROM variables v
WHERE v.label_cn ILIKE :pat
   OR v.name ILIKE :pat
ORDER BY (lower(v.name) = lower(:q)) DESC,
         (v.label_cn ILIKE :pfx) DESC,
         position(lower(:q) IN lower(v.label_cn)),
         v.name
LIMIT :lim
"""

_VAR_SQL = """
SELECT name, label_cn, table_name
FROM variables
WHERE name = :name
"""

_READ_C0 = """
SELECT c.id_org AS firm_id, c.stknme AS firm, c.year AS year,
       c.{col} AS value
FROM panel_c0 c
WHERE c.year BETWEEN :sy AND :ey
  AND c.{col} IS NOT NULL
  {firm_clause}
ORDER BY c.year, c.id_org
LIMIT :lim
"""

_READ_JOIN = """
SELECT c.id_org AS firm_id, c.stknme AS firm, t.year AS year,
       t.{col} AS value
FROM {table} t
JOIN panel_c0 c ON c.id = t.id AND c.year = t.year
WHERE t.year BETWEEN :sy AND :ey
  AND t.{col} IS NOT NULL
  {firm_clause}
ORDER BY t.year, c.id_org
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


def _firm_key(raw: Any) -> str | None:
    """Normalize a firm identifier to the store's key form.

    ``panel_c0.id_org`` is text holding a zero-padded 6-digit code, so an
    integer or short code from a client (``2``) has to be padded to match
    (``000002``); an already-padded code passes through unchanged.
    """
    s = str(raw).strip()
    if not s:
        return None
    if len(s) < 6 and s.isdigit():
        return s.zfill(6)
    return s


def gta_search_variables(query_text: str, limit: int = 50) -> dict:
    """Search GTA panel variables by fuzzy Chinese label or code.

    Returns ``{results, count, truncated}`` with rows
    ``{code, label, brand}`` — storage identity (table, sub-library) is
    not emitted. A domain failure is passed through unchanged.
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
        {"code": r["code"], "label": r["label"], "brand": "finddata"}
        for r in rows[:limit]
    ]
    return {"results": results, "count": len(results), "truncated": len(rows) > limit}


def gta_read(
    variable: str,
    start_year: int | None = None,
    end_year: int | None = None,
    firm_ids: list[int] | None = None,
    limit: int = 200,
) -> dict:
    """Read one panel variable's firm-year values.

    ``firm_ids`` accepts firm codes as returned by this tool (strings) or
    short codes/ints, normalized to the store's 6-digit form. Returns
    ``{results, count, truncated}`` with rows
    ``{firm_id, firm, year, value, brand}`` ordered by year then firm; an
    unknown variable returns ``{"status": "unknown_variable"}``; a year
    range with no data is a truthful empty envelope; a domain failure is
    passed through unchanged.
    """
    name = (variable or "").strip()
    if not name or not _VAR_NAME_RE.match(name):
        return {"status": "unknown_variable", "variable": variable}
    limit = _clamp(limit, _READ_LIMIT_MAX)

    meta = domain_query(_DB, _VAR_SQL, {"name": name})
    if isinstance(meta, dict):
        return meta
    if not meta:
        return {"status": "unknown_variable", "variable": name}
    table = meta[0]["table_name"]
    if table not in _TABLES:
        # Catalog/storage drift: refuse to interpolate an unknown identifier.
        return {"status": "unknown_variable", "variable": name}

    firms = sorted({k for k in (_firm_key(f) for f in (firm_ids or [])) if k})
    firm_clause = "AND c.id_org = ANY(:firms)" if firms else ""
    sql = (_READ_C0 if table == "panel_c0" else _READ_JOIN).format(
        col=name, firm_clause=firm_clause,
        **({"table": table} if table != "panel_c0" else {}))
    params: dict[str, Any] = {
        "sy": start_year if start_year is not None else -2147483648,
        "ey": end_year if end_year is not None else 2147483647,
        "lim": limit + 1,
    }
    if firms:
        params["firms"] = firms
    rows = domain_query(_DB, sql, params)
    if isinstance(rows, dict):
        return rows
    truncated = len(rows) > limit
    results = [
        {"firm_id": r["firm_id"], "firm": r["firm"], "year": r["year"],
         "value": _num(r["value"]), "brand": "finddata"}
        for r in rows[:limit]
    ]
    return {"results": results, "count": len(results), "truncated": truncated}
