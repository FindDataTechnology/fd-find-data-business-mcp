"""fd-find-data-business-mcp — commercial, login-gated FindData MCP server.

Thin shell over fd-open-data-mcp: reuses its cache, dispatch, ontology, and
search as a library, but exposes zero upstream-provider identity. Every served
value is branded ``finddata``. Auth is Logto JWT via FastMCP's ``JWTVerifier``.
"""
from __future__ import annotations

import os

from fastmcp import FastMCP

from fd_open_data_mcp.db import get_database

# --- Auth: Logto JWT, env-gated ---------------------------------------------
# Unset FDBIZ_JWKS_URI => no auth (local dev / smoke test). Set in deploy to
# reject every request without a valid Logto-issued JWT (401 before any tool).
# Algorithm defaults to ES384 (Logto's signing key); override via FDBIZ_ALGORITHM.
def _build_auth():
    jwks = os.environ.get("FDBIZ_JWKS_URI")
    if not jwks:
        return None
    # fastmcp >= 3.x: JWTVerifier in fastmcp.server.auth.providers.jwt
    try:
        from fastmcp.server.auth.providers.jwt import JWTVerifier
    except ImportError:
        from fastmcp.server.auth import JWTVerifier  # older fastmcp
    return JWTVerifier(
        jwks_uri=jwks,
        issuer=os.environ.get("FDBIZ_ISSUER"),
        audience=os.environ.get("FDBIZ_AUDIENCE"),
        algorithm=os.environ.get("FDBIZ_ALGORITHM", "ES384"),
    )


mcp = FastMCP(
    name="finddata",
    instructions=(
        "FindData commercial data service. Discover indicators with ai_search "
        "(natural-language; superset of all search tools — call it once, not in "
        "parallel with the others). Read a value with read(concept_id, "
        "entity_type, entity_id, dates); bulk series with read_range. Browse "
        "all indicators with list_concepts; explore entity relationships with "
        "graph_search. No source/provider attribution is exposed — every value "
        "is FindData-branded."
    ),
    auth=_build_auth(),
)


def _session():
    return get_database().get_session()


# --- Brand isolation: strip upstream-provider identity from any payload ------
_PROVENANCE_KEYS = {"source_used", "real_source_used", "source", "real_source"}


def _strip(row):
    """Recursively drop upstream-provider keys from a response payload."""
    if isinstance(row, dict):
        return {k: _strip(v) for k, v in row.items() if k not in _PROVENANCE_KEYS}
    if isinstance(row, list):
        return [_strip(x) for x in row]
    return row


def _coerce(v):
    """Cached values are strings; coerce numerics back so reads return numbers."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


# --- Read: serve crawled cache only (fresh OR stale); provider-independent ----
@mcp.tool
def read(concept_id: int, entity_type: str, entity_id: int, dates: list[str]) -> list[dict]:
    """Read a FindData indicator for an entity over a list of dates.

    Serves any cached observation (fresh or stale) — stability over recency, and
    zero runtime dependency on upstream providers: the read path never
    dispatches to akshare/wbgapi/etc.; it reads only FindData's crawled store.
    A never-crawled date returns ``unavailable``. No value is fabricated.
    """
    from fd_open_data_mcp.fetch.cache import read_cache

    # ponytail: cap per upstream read(); a commercial caller paging huge date
    # lists should use read_range, not explode one call.
    MAX_DATES = 366
    if len(dates) > MAX_DATES:
        dates = dates[:MAX_DATES]

    s = _session()
    try:
        out: list[dict] = []
        for d in dates:
            o = read_cache(s, concept_id, entity_type, entity_id, d)
            if o is not None:
                # Serve what we have — fresh or stale. No re-fetch, no dispatch.
                out.append({"date": d, "value": _coerce(o.value), "unit": o.unit,
                            "brand": "finddata"})
            else:
                out.append({"date": d, "value": None, "status": "unavailable"})
        return out
    finally:
        s.close()


# --- Read range: bulk series, same strip + stale-serve ---------------------
@mcp.tool
def read_range(
    concept_ids: list[int], entity_type: str, entity_id: int,
    start: str, end: str,
) -> dict[int, list[dict]]:
    """Bulk-read one or more indicators for an entity over ``[start, end]``.

    Cached rows (fresh or stale) are served; a cold range (no rows) is fetched
    synchronously and recorded. Returns ``{concept_id: [{date, value, unit,
    brand: "finddata"}, ...]}``; concepts with no data get an empty list.
    """
    from fd_open_data_mcp.fetch.cache import read_cache_range
    from fd_open_data_mcp.fetch.dispatch import read_range as _read_range

    s = _session()
    try:
        out: dict[int, list[dict]] = {}
        for cid in concept_ids:
            cached = read_cache_range(s, cid, entity_type, entity_id, start, end)
            if cached:
                out[cid] = [
                    {"date": r.date, "value": _coerce(r.value), "unit": r.unit,
                     "brand": "finddata"}
                    for r in cached
                ]
            else:
                # Cold range → one ranked range fetch, bulk-write, return.
                fetched = _read_range(s, [cid], entity_type, entity_id, start, end)
                out[cid] = [
                    {"date": r["date"], "value": _coerce(r["value"]), "unit": r["unit"],
                     "brand": "finddata"}
                    for r in fetched.get(cid, [])
                ]
        return out
    finally:
        s.close()


# --- Full catalog visibility (provenance-free) ------------------------------
@mcp.tool
def list_concepts(entity_type: str | None = None) -> list[dict]:
    """List all available FindData indicators, optionally filtered by entity type."""
    from fd_open_data_mcp.models import Concept

    s = _session()
    try:
        q = s.query(Concept)
        if entity_type:
            q = q.filter_by(entity_type=entity_type)
        return _strip([c.toDict() for c in q.limit(500).all()])
    finally:
        s.close()


@mcp.tool
def ai_search(
    query: str,
    entity_type: str | None = None,
    limit: int = 20,
    include_values: bool = False,
    value_date: str | None = None,
) -> dict:
    """Natural-language indicator + entity discovery (superset of all search tools).

    Call ONCE; do not also call semantic_search/semantic_search_entities in
    parallel — they overlap and return duplicate data. FindData-branded; no
    source attribution in results.
    """
    from fd_open_data_mcp.ai_search import ai_search as _ai_search

    return _strip(_ai_search(query, entity_type, limit, include_values, value_date))


@mcp.tool
def graph_search(
    algorithm: str,
    start_entity_code: str,
    end_entity_code: str | None = None,
    max_depth: int = 3,
    entity_type_filter: str | None = None,
) -> dict:
    """Entity-relationship graph queries (NetworkX): bfs, dfs, neighbors,
    shortest_path, subgraph, ego_graph, statistics."""
    from fd_open_data_mcp.graph.manager import EntityGraphManager

    db = get_database()
    gm = EntityGraphManager(db.database_url)
    valid = {"bfs", "dfs", "neighbors", "shortest_path", "subgraph", "ego_graph", "statistics"}
    if algorithm not in valid:
        return {"error": f"Invalid algorithm: {algorithm}. Valid: {sorted(valid)}"}
    if algorithm == "statistics":
        return _strip(gm.get_statistics())
    if not start_entity_code:
        return {"error": "start_entity_code is required"}
    start = gm.find_node_by_code(start_entity_code, entity_type_filter)
    if start is None:
        return {"error": f"Entity not found: {start_entity_code}"}
    if algorithm == "bfs":
        r = gm.bfs_traversal(start, max_depth, entity_type_filter)
        return {"algorithm": "bfs", "start_entity": start_entity_code, "count": len(r), "results": _strip(r)}
    if algorithm == "dfs":
        r = gm.dfs_traversal(start, max_depth, entity_type_filter)
        return {"algorithm": "dfs", "start_entity": start_entity_code, "count": len(r), "results": _strip(r)}
    if algorithm == "neighbors":
        r = gm.get_neighbors(start, entity_type_filter)
        return {"algorithm": "neighbors", "entity": start_entity_code, "count": len(r), "results": _strip(r)}
    if algorithm == "shortest_path":
        if not end_entity_code:
            return {"error": "end_entity_code is required for shortest_path"}
        end = gm.find_node_by_code(end_entity_code, entity_type_filter)
        if end is None:
            return {"error": f"Entity not found: {end_entity_code}"}
        r = gm.shortest_path(start, end)
        return {"algorithm": "shortest_path", "start_entity": start_entity_code,
                "end_entity": end_entity_code, "path_length": max(0, len(r) - 1),
                "results": _strip(r)}
    if algorithm == "subgraph":
        if not entity_type_filter:
            return {"error": "entity_type_filter is required for subgraph"}
        r = gm.get_subgraph_by_type(entity_type_filter, max_nodes=100)
        return {"algorithm": "subgraph", "entity_type": entity_type_filter,
                "node_count": len(r["nodes"]), "edge_count": len(r["edges"]),
                "nodes": _strip(r["nodes"]), "edges": _strip(r["edges"])}
    if algorithm == "ego_graph":
        r = gm.get_ego_graph(start, radius=max_depth)
        return {"algorithm": "ego_graph", "center_entity": start_entity_code,
                "radius": max_depth, "node_count": len(r["nodes"]),
                "edge_count": len(r["edges"]), "nodes": _strip(r["nodes"]),
                "edges": _strip(r["edges"])}
    return {"error": f"Unhandled algorithm: {algorithm}"}


# --- Domain federation: yearbook + law + world bank + GTA + city panel -------
# Read-only federation over FDBIZ_DOMAIN_PG_URL. Fail-soft by design: when the
# data machine is down these tools return {"status": "domain_unavailable"}
# while every core tool above is unaffected (separate engines and timeouts).

@mcp.tool
def yearbook_search_indicators(query: str, limit: int = 50) -> dict:
    """Search FindData statistical-yearbook indicators by fuzzy name.

    Returns {results: [{id, name, category, unit, brand}], count, truncated}.
    Chinese or English partial names both work. If the yearbook store is
    unreachable, returns {"status": "domain_unavailable"} instead of failing.
    """
    from fd_find_data_business_mcp.tools_yearbook import (
        yearbook_search_indicators as _impl)

    return _strip(_impl(query, limit))


@mcp.tool
def yearbook_read(
    indicator_id: int,
    region: str | None = None,
    start_year: int | None = None,
    end_year: int | None = None,
    limit: int = 200,
) -> dict:
    """Read a yearbook indicator's annual series, optionally for one region
    and year range.

    Returns {results: [{year, region, value, unit, brand}], count, truncated};
    a filter with no data returns an empty results list (truthful, not an
    error). Latest edition wins when multiple yearbook editions cover the
    same region-year.
    """
    from fd_find_data_business_mcp.tools_yearbook import yearbook_read as _impl

    return _strip(_impl(indicator_id, region, start_year, end_year, limit))


@mcp.tool
def law_search(title_query: str, category: str | None = None, limit: int = 50) -> dict:
    """Search Chinese laws and regulations by fuzzy title, optionally
    filtered by category (e.g. 法律, 行政法规, 司法解释, 地方性法规).

    Returns metadata rows only ({results: [{id, title, category, type,
    status, publish, expiry, brand}], count, truncated}); fetch full text
    with law_read.
    """
    from fd_find_data_business_mcp.tools_law import law_search as _impl

    return _strip(_impl(title_query, category, limit))


@mcp.tool
def law_read(law_id: int) -> dict:
    """Read one law's full text and metadata by id (from law_search).

    Returns {id, title, category, type, status, publish, expiry, content,
    brand}; an unknown id returns {"status": "not_found"}.
    """
    from fd_find_data_business_mcp.tools_law import law_read as _impl

    return _strip(_impl(law_id))


@mcp.tool
def wb_search_indicators(query: str, limit: int = 50) -> dict:
    """Search World Bank (WDI) indicators by fuzzy name, topic, or code.

    Returns {results: [{code, name, name_en, topic, unit, brand}], count,
    truncated}. Chinese and English names both work. If the domain store is
    unreachable, returns {"status": "domain_unavailable"} instead of failing.
    """
    from fd_find_data_business_mcp.tools_wb import (
        wb_search_indicators as _impl)

    return _strip(_impl(query, limit))


@mcp.tool
def wb_read(
    indicator_code: str,
    countries: list[str],
    start_year: int | None = None,
    end_year: int | None = None,
    limit: int = 600,
) -> dict:
    """Read a World Bank indicator's annual series for a list of countries.

    ``countries`` accepts ISO3 codes (CHN) or Chinese names (中国). Returns
    {results: [{year, country, country_name, value, unit, brand}], count,
    truncated}; missing country-year cells are absent (truthful, never
    fabricated); an unknown indicator or country returns an empty list.
    """
    from fd_find_data_business_mcp.tools_wb import wb_read as _impl

    return _strip(_impl(indicator_code, countries, start_year, end_year, limit))


@mcp.tool
def gta_search_variables(query: str, limit: int = 50) -> dict:
    """Search GTA listed-company panel variables by fuzzy Chinese label or code.

    Returns {results: [{code, label, brand}], count, truncated} — storage
    identity (table, sub-library) is not exposed. If the domain store is
    unreachable, returns {"status": "domain_unavailable"}.
    """
    from fd_find_data_business_mcp.tools_gta import (
        gta_search_variables as _impl)

    return _strip(_impl(query, limit))


@mcp.tool
def gta_read(
    variable: str,
    start_year: int | None = None,
    end_year: int | None = None,
    firm_ids: list[int] | None = None,
    limit: int = 200,
) -> dict:
    """Read one GTA panel variable's firm-year values.

    Returns {results: [{firm_id, firm, year, value, brand}], count,
    truncated}, ordered by year then firm; an unknown variable returns
    {"status": "unknown_variable"}; an empty year range is a truthful empty
    list. Use gta_search_variables first to find variable codes.
    """
    from fd_find_data_business_mcp.tools_gta import gta_read as _impl

    return _strip(_impl(variable, start_year, end_year, firm_ids, limit))


@mcp.tool
def city_search_variables(query: str, limit: int = 50) -> dict:
    """Search China city-panel variables by fuzzy Chinese name or code.

    Returns {results: [{code, label, unit, brand}], count, truncated}. If
    the domain store is unreachable, returns {"status": "domain_unavailable"}.
    """
    from fd_find_data_business_mcp.tools_city import (
        city_search_variables as _impl)

    return _strip(_impl(query, limit))


@mcp.tool
def city_read(
    variable: str,
    city: str | None = None,
    start_year: int | None = None,
    end_year: int | None = None,
    limit: int = 2000,
) -> dict:
    """Read one city-panel variable's city-year values (297 cities, 2000-2024).

    ``city`` accepts a city code or Chinese name. Returns
    {results: [{year, city_code, city_name, value, brand}], count,
    truncated}; an unknown variable returns {"status": "unknown_variable"};
    a filter with no data is a truthful empty list.
    """
    from fd_find_data_business_mcp.tools_city import city_read as _impl

    return _strip(_impl(variable, city, start_year, end_year, limit))


# --- Entry point ------------------------------------------------------------
def main(transport: str = "stdio", host: str = "127.0.0.1", port: int = 8310) -> None:
    """Run the FindData business MCP server.

    stdio (default) for local subprocess clients; ``http`` for remote
    login-gated serving at ``http://<host>:<port>/mcp``. When
    ``FDBIZ_JWKS_URI`` is set, every HTTP request must carry a valid Logto
    JWT (401 otherwise).
    """
    if transport == "stdio":
        mcp.run()
        return
    mcp.run(transport=transport, host=host, port=port)


if __name__ == "__main__":
    main()
