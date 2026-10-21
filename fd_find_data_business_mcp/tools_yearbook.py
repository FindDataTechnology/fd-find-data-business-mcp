"""Yearbook tools — federated, read-only access to the yearbook catalog.

``yearbook_search_indicators`` discovers indicators by fuzzy name over the
standardized catalog (indicators + aliases); ``yearbook_read`` serves the
latest-edition series for one indicator from indicator_values. Both go
through the fail-soft domain layer and brand every served row ``finddata``;
no table_id, dims, or provenance key is ever emitted. Search rows carry the
unified indicator-registry identity (``native_code`` + ``semantic_code``),
enriched via a separate registry lookup that fails soft to null.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from fd_find_data_business_mcp.domain import (
    query as domain_query, registry_semantic_codes)

_DB = "yearbook_catalog"

#: Registry anchor for this store: native_code is the indicators.id cast to
#: text (the registry stores text anchors for every source).
_REGISTRY_ANCHOR = ("yearbook_catalog", "indicators", "id")

_SEARCH_LIMIT_MAX = 200
_READ_LIMIT_MAX = 1000
_REGIONS_LIMIT_MAX = 500

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

# DISTINCT ON requires ORDER BY to start with (region, data_year); the outer
# query re-sorts by (data_year, region) so pagination is deterministic and
# year-major for consumers.
_READ_SELECT = """
SELECT data_year, region, value_num, value, unit
FROM (
    SELECT DISTINCT ON (region, data_year)
           data_year, region, value_num, value, unit
    FROM indicator_values
    WHERE {conds}
    ORDER BY region, data_year, edition_year DESC NULLS LAST,
             (value_num IS NULL), id
) latest
ORDER BY data_year, region COLLATE "C"
LIMIT :lim OFFSET :off
"""

_REGIONS_SQL = """
SELECT region, dims->>'admin_region' AS province, count(*) AS row_count
FROM indicator_values
WHERE indicator_id = :iid
GROUP BY region, dims->>'admin_region'
ORDER BY province NULLS LAST, region NULLS LAST
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
    ``{id, name, category, unit, native_code, semantic_code, brand}``:
    ``native_code`` repeats the registry anchor (the id as text) and
    ``semantic_code`` carries the unified indicator-registry identity —
    null when the entry is unverified or unregistered (registry lookups
    fail soft, exactly like the main query). A domain failure is passed
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
    page = rows[:limit]
    native_codes = [str(r["id"]) for r in page]
    semantic = registry_semantic_codes(*_REGISTRY_ANCHOR, native_codes)
    results = [
        {"id": r["id"], "name": r["name"], "category": r["category"],
         "unit": r["unit"], "native_code": str(r["id"]),
         "semantic_code": semantic.get(str(r["id"])), "brand": "finddata"}
        for r in page
    ]
    return {"results": results, "count": len(results), "truncated": len(rows) > limit}


def yearbook_read(
    indicator_id: int,
    region: str | None = None,
    start_year: int | None = None,
    end_year: int | None = None,
    limit: int = 200,
    offset: int = 0,
    province: str | None = None,
) -> dict:
    """Read the value series of one yearbook indicator.

    Serves one row per (region, data_year) — the latest ``edition_year``
    only, chosen in SQL with DISTINCT ON. Within the same edition a row
    with a numeric value outranks text siblings (English annotations and
    header residue are ingested from parallel columns); a text row is
    served only when no numeric sibling exists for that cell.

    ``region`` matches the ``region`` column directly, and additionally
    matches the ``dims.admin_region`` dimension when the column carries no
    region (province-level values are ingested with the region held in the
    dimension and an empty column). The guard matters: cross-region
    comparison tables put the ROW region in the column while the dimension
    holds the COLUMN region — an unguarded OR would serve those mislabeled
    siblings.

    ``province`` matches ``dims->>'admin_region'`` regardless of whether
    the ``region`` column is empty — the supported way to select all
    county/city rows of a province. It is combinable with ``region`` and
    the year range, and never relaxes the ``region`` guard above.

    Rows are ordered deterministically by ``(data_year, region)``. ``offset``
    pages through the full result set; when more rows remain the response
    carries ``truncated: true`` and ``next_offset``. Returns
    ``{results, count, truncated, next_offset}``; no rows is a truthful
    empty envelope, and a domain failure is passed through unchanged.
    """
    limit = _clamp(limit, _READ_LIMIT_MAX)
    offset = max(0, int(offset or 0))
    conds = ["indicator_id = :iid"]
    params: dict[str, Any] = {"iid": indicator_id, "lim": limit + 1, "off": offset}
    if region is not None:
        # Match the region column directly; fall back to the dimension only
        # when the column carries no region (province values are ingested
        # with the dimension holding the region and an empty column). The
        # guard matters: cross-region comparison tables put the ROW region
        # in the column while the dimension holds the COLUMN region — an
        # unguarded OR would serve those mislabeled siblings.
        conds.append(
            "(region = :region OR ((region IS NULL OR btrim(region) = '')"
            " AND dims->>'admin_region' = :region))")
        params["region"] = region
    if province is not None:
        # Separate parameter: selects every county/city row of a province
        # even when the region column is non-empty. It must not widen the
        # region guard above (that guard protects cross-region comparison
        # tables).
        conds.append("dims->>'admin_region' = :province")
        params["province"] = province
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
    results.sort(key=lambda row: (row["year"], (row["region"] or "").encode("utf-8")))
    return {
        "results": results,
        "count": len(results),
        "truncated": truncated,
        "next_offset": offset + limit if truncated else None,
    }


def yearbook_regions(indicator_id: int, limit: int = 500) -> dict:
    """List the available (province, region) pairs of one indicator.

    Returns ``{results: [{province, region, row_count}], count, truncated}``
    ordered by ``(province, region)``. ``province`` comes from the
    ``dims.admin_region`` dimension; ``region`` is the region column (null
    or empty for province-level values). The list is bounded by ``limit``
    (max 500); a domain failure is passed through unchanged.
    """
    limit = _clamp(limit, _REGIONS_LIMIT_MAX)
    rows = domain_query(_DB, _REGIONS_SQL, {"iid": indicator_id, "lim": limit + 1})
    if isinstance(rows, dict):
        return rows
    truncated = len(rows) > limit
    results = [
        {"province": r["province"], "region": r["region"],
         "row_count": int(r["row_count"])}
        for r in rows[:limit]
    ]
    return {"results": results, "count": len(results), "truncated": truncated}
