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
    SELECT DISTINCT ON (iv.region, iv.data_year)
           iv.data_year, iv.region, iv.value_num, iv.value, iv.unit
    FROM indicator_values iv
    WHERE {conds}
    ORDER BY iv.region, iv.data_year, iv.edition_year DESC NULLS LAST,
             (iv.value_num IS NULL), iv.id
) latest
ORDER BY data_year, region COLLATE "C"
LIMIT :lim OFFSET :off
"""

# Province resolution is row-level: a row carrying dims.admin_region uses it,
# and only a row without one falls back to the materialized
# (indicator, region) -> province map. The branches are mutually exclusive on
# purpose — an unguarded OR would pull a multi-province table's generic rows
# (地区/市/市辖区) into whatever province the map happens to hold.
_PROVINCE_PREDICATE = """(
    (NULLIF(btrim(iv.dims->>'admin_region'), '') IS NOT NULL
     AND btrim(iv.dims->>'admin_region') = ANY(:prov_names))
    OR
    (NULLIF(btrim(iv.dims->>'admin_region'), '') IS NULL
     AND EXISTS (
         SELECT 1 FROM yb_indicator_region_province m
         WHERE m.indicator_id = iv.indicator_id
           AND m.region IS NOT DISTINCT FROM iv.region
           AND m.province_code = :prov_code))
)"""

_PROVINCE_LOOKUP_SQL = """
SELECT code, name_zh, aliases
FROM yb_province_dict
WHERE lower(code) = lower(:t) OR name_zh = :t OR :t = ANY(aliases)
LIMIT 1
"""

_REGIONS_SQL = """
SELECT iv.region AS region,
       COALESCE(dp.name_zh, NULLIF(btrim(iv.dims->>'admin_region'), ''),
                d.name_zh) AS province,
       COALESCE(dp.code, m.province_code) AS province_code,
       count(*) AS row_count
FROM indicator_values iv
LEFT JOIN LATERAL (
    SELECT pd.code, pd.name_zh
    FROM yb_province_dict pd
    WHERE NULLIF(btrim(iv.dims->>'admin_region'), '') IS NOT NULL
      AND (pd.name_zh = btrim(iv.dims->>'admin_region')
           OR btrim(iv.dims->>'admin_region') = ANY(pd.aliases))
    LIMIT 1
) dp ON true
LEFT JOIN yb_indicator_region_province m
       ON m.indicator_id = iv.indicator_id
      AND m.region IS NOT DISTINCT FROM iv.region
LEFT JOIN yb_province_dict d ON d.code = m.province_code
WHERE iv.indicator_id = :iid
GROUP BY iv.region,
         COALESCE(dp.name_zh, NULLIF(btrim(iv.dims->>'admin_region'), ''),
                  d.name_zh),
         COALESCE(dp.code, m.province_code)
ORDER BY COALESCE(dp.name_zh, NULLIF(btrim(iv.dims->>'admin_region'), ''),
                  d.name_zh) COLLATE "C" NULLS LAST,
         iv.region COLLATE "C" NULLS LAST
LIMIT :lim
"""

_REGION_DICT_SQL = """
SELECT code, name_zh AS name, aliases
FROM yb_province_dict
WHERE code = ANY(:codes)
ORDER BY name_zh COLLATE "C"
"""


def _resolve_province(text: str) -> tuple[dict | None, dict | None]:
    """Resolve a province name, two-letter code or alias to its dictionary row.

    Returns ``({code, names}, None)`` on a hit, ``(None, None)`` for an unknown
    name (the caller falls back to a raw dimension match), and
    ``(None, <domain error>)`` when the dictionary itself is unreachable.
    """
    rows = domain_query(_DB, _PROVINCE_LOOKUP_SQL, {"t": text})
    if isinstance(rows, dict):
        return None, rows
    if not rows:
        return None, None
    row = rows[0]
    names = [row["name_zh"], *(row.get("aliases") or [])]
    return {"code": row["code"], "names": names}, None


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

    ``province`` accepts a province name, a ``yb_province_dict`` two-letter
    code or an alias (e.g. ``山西`` / ``sx`` / ``山西省``) — the supported way
    to select all county/city rows of a province. A row that carries
    ``dims.admin_region`` is matched on the dimension; only a row without one
    falls back to the materialized ``(indicator, region)`` province map, so a
    multi-province table's generic rows are never swept into a province they
    do not hold. It is combinable with ``region`` and the year range, and
    never relaxes the ``region`` guard above.

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
            "(iv.region = :region OR ((iv.region IS NULL OR btrim(iv.region) = '')"
            " AND iv.dims->>'admin_region' = :region))")
        params["region"] = region
    if province is not None:
        # A province selects every county/city row of that province, whether
        # the region column carries a county name or nothing at all. The
        # filter resolves name/code/alias through yb_province_dict and then
        # joins the materialized (indicator, region) map; it must not widen
        # the region guard above (that guard protects cross-region
        # comparison tables).
        prov_text = province.strip()
        if prov_text:
            resolved, failure = _resolve_province(prov_text)
            if failure is not None:
                return failure
            if resolved is None:
                resolved = {"code": None, "names": [prov_text]}
            conds.append(_PROVINCE_PREDICATE)
            params["prov_names"] = resolved["names"]
            params["prov_code"] = resolved["code"]
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

    Returns ``{results: [{province, province_code, region, row_count}],
    provinces: [{code, name, aliases}], count, truncated}`` ordered by
    ``(province, region)``. ``province`` is resolved row-level: the
    ``dims.admin_region`` dimension when the row carries it, otherwise the
    materialized ``(indicator, region)`` map (the source where province-level
    values keep an empty region column). ``provinces`` is the bounded
    dictionary slice covering the returned rows, so a caller can normalise a
    province name/alias/code before reading. The list is bounded by ``limit``
    (max 500); a domain failure is passed through unchanged.
    """
    limit = _clamp(limit, _REGIONS_LIMIT_MAX)
    rows = domain_query(_DB, _REGIONS_SQL, {"iid": indicator_id, "lim": limit + 1})
    if isinstance(rows, dict):
        return rows
    truncated = len(rows) > limit
    results = [
        {"province": r["province"], "province_code": r["province_code"],
         "region": r["region"], "row_count": int(r["row_count"])}
        for r in rows[:limit]
    ]
    codes = sorted({r["province_code"] for r in rows if r.get("province_code")})
    provinces: list[dict] = []
    if codes:
        dict_rows = domain_query(_DB, _REGION_DICT_SQL, {"codes": codes})
        if isinstance(dict_rows, dict):
            return dict_rows
        provinces = [
            {"code": r["code"], "name": r["name"],
             "aliases": list(r.get("aliases") or [])}
            for r in dict_rows
        ]
    return {"results": results, "provinces": provinces,
            "count": len(results), "truncated": truncated}
