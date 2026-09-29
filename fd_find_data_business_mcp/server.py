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
#
# Service-to-service lane: FDBIZ_INTERNAL_TOKEN (when set) is accepted as a
# bearer credential in its own right, in addition to Logto JWTs. This is how
# fd-open-data-mcp's transparent registry reads reach this server (its
# FD_MCP_BUSINESS_TOKEN) — a fixed shared secret over the tailnet, not a
# second IdP. Any other value still goes through JWKS verification.
def _build_auth():
    jwks = os.environ.get("FDBIZ_JWKS_URI")
    if not jwks:
        return None
    # fastmcp >= 3.x: JWTVerifier in fastmcp.server.auth.providers.jwt
    try:
        from fastmcp.server.auth.providers.jwt import JWTVerifier
    except ImportError:
        from fastmcp.server.auth import JWTVerifier  # older fastmcp

    internal_token = os.environ.get("FDBIZ_INTERNAL_TOKEN", "").strip()
    if not internal_token:
        return JWTVerifier(
            jwks_uri=jwks,
            issuer=os.environ.get("FDBIZ_ISSUER"),
            audience=os.environ.get("FDBIZ_AUDIENCE"),
            algorithm=os.environ.get("FDBIZ_ALGORITHM", "ES384"),
        )

    import hmac as _hmac

    from fastmcp.server.auth.auth import AccessToken

    class _InternalAwareVerifier(JWTVerifier):
        async def verify_token(self, token: str):
            if _hmac.compare_digest(token.encode(), internal_token.encode()):
                return AccessToken(
                    token=token,
                    client_id="fd-open-data-mcp-federation",
                    scopes=[],
                    expires_at=None,
                    subject="fd-open-data-mcp",
                )
            return await super().verify_token(token)

    return _InternalAwareVerifier(
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
    # Shared singleton engine (same TTL rules as fd-open-data-mcp; the graph
    # is not rebuilt between calls — spec semantic-search delegated retrieval).
    from fd_open_data_mcp import engines

    gm = engines.get_graph_manager()
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

# Scope gate (indicator-scope): each domain tool IS one source ("源即 scope",
# shared-library design D4) — a scope that excludes this tool's source yields
# an explainable empty result; an unknown scope is an explicit error; a
# permitted scoped call is disclosed + counted. Sources here use the same
# names scopes are written with (registry source_dbs, plus the document-domain
# codes that scopes may pin).
_TOOL_SOURCES = {
    "yearbook": ["yearbook_catalog"],
    "wb": ["world_bank"],
    "gta": ["gta_panel"],
    "city": ["china_city_panel"],
    "law": ["law_db"],
    "legal_docs": [
        "gov-rules-library", "mfa-treaty-library", "ccdi-supervisory-regulations",
        "rmfyalk-case-library", "party-regulation-library",
    ],
    "guide_cases": ["guide_cases"],
    "wenshu": ["wenshu_db"],
    "ia35": ["ia35"],
}


def _scope_gate(scope: str | None, tool_key: str) -> tuple[dict | None, dict | None]:
    """Resolve + gate a domain tool's optional ``scope`` parameter.

    Returns ``(scope_meta, gate_response)``; ``gate_response`` is not None
    when the call must return it as-is (unknown scope / source outside the
    scope). Scoped-but-permitted calls return the metadata to attach; stats
    are recorded by the caller once the result size is known.
    """
    from fd_open_data_mcp import scoping

    s = _session()
    try:
        try:
            return scoping.domain_scope_gate(s, scope, _TOOL_SOURCES[tool_key])
        except scoping.ScopeNotFound as exc:
            return None, {"error": "unknown_scope", "scope": exc.name, "detail": str(exc)}
    finally:
        s.close()


def _scope_record(meta: dict | None, result_count: int) -> None:
    """Count a served scoped retrieval (design D6)."""
    if not meta:
        return
    from fd_open_data_mcp import scoping

    s = _session()
    try:
        scoping.record_hit(s, meta["name"], result_count)
    finally:
        s.close()


@mcp.tool
def yearbook_search_indicators(query: str, limit: int = 50,
                               scope: str | None = None) -> dict:
    """Search FindData statistical-yearbook indicators by fuzzy name.

    Returns {results: [{id, name, category, unit, native_code,
    semantic_code, brand}], count, truncated}. Chinese or English partial
    names both work. native_code repeats the registry anchor (the id) and
    semantic_code carries the unified indicator-registry identity — null
    when the entry is unverified or unregistered. If the yearbook store is
    unreachable, returns {"status": "domain_unavailable"} instead of failing.
    ``scope``: named retrieval scope — an unknown scope errors explicitly; a
    scope excluding the yearbook source returns an explainable empty result;
    a scoped response names the scope in effect.
    """
    from fd_find_data_business_mcp.tools_yearbook import (
        yearbook_search_indicators as _impl)

    meta, gate = _scope_gate(scope, "yearbook")
    if gate is not None:
        return gate
    out = _strip(_impl(query, limit))
    if meta:
        out["scope"] = meta
        _scope_record(meta, out.get("count", 0))
    return out


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
def law_search(title_query: str, category: str | None = None, limit: int = 50,
               scope: str | None = None) -> dict:
    """Search Chinese laws and regulations by fuzzy title, optionally
    filtered by category (e.g. 法律, 行政法规, 司法解释, 地方性法规).

    Returns metadata rows only ({results: [{id, title, category, type,
    status, publish, expiry, brand}], count, truncated}); fetch full text
    with law_read. ``scope``: named retrieval scope — unknown scope errors
    explicitly; a scope excluding the law source returns an explainable
    empty result; a scoped response names the scope in effect.
    """
    from fd_find_data_business_mcp.tools_law import law_search as _impl

    meta, gate = _scope_gate(scope, "law")
    if gate is not None:
        return gate
    out = _strip(_impl(title_query, category, limit))
    if meta:
        out["scope"] = meta
        _scope_record(meta, out.get("count", 0))
    return out


@mcp.tool
def law_read(law_id: int) -> dict:
    """Read one law's full text and metadata by id (from law_search).

    Returns {id, title, category, type, status, publish, expiry, content,
    brand}; an unknown id returns {"status": "not_found"}.
    """
    from fd_find_data_business_mcp.tools_law import law_read as _impl

    return _strip(_impl(law_id))


@mcp.tool
def legal_docs_search(
    title_query: str,
    source: str | None = None,
    doc_type: str | None = None,
    limit: int = 50,
    scope: str | None = None,
) -> dict:
    """Search the authoritative legal-documents corpus by fuzzy title.

    Covers five crawled sources: gov-rules-library (部门/地方规章),
    mfa-treaty-library (条约), ccdi-supervisory-regulations (纪检监察
    法规), rmfyalk-case-library (人民法院案例库), party-regulation-library
    (党内法规). Filter by ``source`` (exact five-source code) and/or
    ``doc_type``. Returns metadata only ({results: [{id, title, source,
    source_label_zh, doc_type, status, authority, language, published_at,
    effective_at, has_content, brand}], count, truncated}); fetch full text
    with legal_docs_read. An unknown source code returns
    {"status": "invalid_filter", "valid_sources": [...]}. ``scope``: named
    retrieval scope — unknown scope errors explicitly; a scope excluding all
    five sources returns an explainable empty result; a scoped response
    names the scope in effect.
    """
    from fd_find_data_business_mcp.tools_legal_docs import (
        legal_docs_search as _impl)

    meta, gate = _scope_gate(scope, "legal_docs")
    if gate is not None:
        return gate
    out = _strip(_impl(title_query, source, doc_type, limit))
    if meta:
        out["scope"] = meta
        _scope_record(meta, out.get("count", 0))
    return out


@mcp.tool
def legal_docs_read(doc_id: int) -> dict:
    """Read one legal document's full text and metadata by id (from legal_docs_search).

    Returns metadata + {content_available, content?, brand}. When the body
    was never fetched (source-side async rendering or oversized attachment —
    most ccdi-supervisory entries), the response carries
    ``content_available: false`` with a note and no ``content`` key; it is
    never an error and never an empty string. An unknown id returns
    {"status": "not_found"}.
    """
    from fd_find_data_business_mcp.tools_legal_docs import (
        legal_docs_read as _impl)

    return _strip(_impl(doc_id))


@mcp.tool
def guide_cases_search(
    title_query: str,
    doc_type: str | None = None,
    reference_status: str | None = None,
    limit: int = 50,
    scope: str | None = None,
) -> dict:
    """Search Supreme People's Court guiding cases and batch notices by fuzzy title.

    ``doc_type`` filters case/notice; ``reference_status`` filters
    有效/不再参照 (withdrawn cases stay searchable — the notice withdraws
    reference effect, not the document). Every row carries
    ``reference_status`` so consumers can see effect at a glance. Returns
    {results: [{source_id, case_no, doc_type, title, listed_at, publish_at,
    source, reference_status, brand}], count, truncated}; fetch structured
    sections with guide_cases_read. Invalid filter values return
    {"status": "invalid_filter"} with the accepted values. ``scope``: named
    retrieval scope — unknown scope errors explicitly; a scope excluding the
    guiding-cases source returns an explainable empty result; a scoped
    response names the scope in effect.
    """
    from fd_find_data_business_mcp.tools_guide_cases import (
        guide_cases_search as _impl)

    meta, gate = _scope_gate(scope, "guide_cases")
    if gate is not None:
        return gate
    out = _strip(_impl(title_query, doc_type, reference_status, limit))
    if meta:
        out["scope"] = meta
        _scope_record(meta, out.get("count", 0))
    return out


@mcp.tool
def guide_cases_read(case_id: str) -> dict:
    """Read one guiding case or notice by source_id (from guide_cases_search).

    Returns full metadata + the published structured sections
    (keyword/gist/facts/result/reasoning/statutes — unpublished sections are
    absent, never fabricated) + ``body`` full text + ``reference_status``
    with ``reference_note`` (the notice number that declared 不再参照).
    An unknown id returns {"status": "not_found"}.
    """
    from fd_find_data_business_mcp.tools_guide_cases import (
        guide_cases_read as _impl)

    return _strip(_impl(case_id))


@mcp.tool
def wenshu_search(
    title_query: str,
    doc_type: str | None = None,
    province: str | None = None,
    start_year: int | None = None,
    end_year: int | None = None,
    limit: int = 50,
    scope: str | None = None,
) -> dict:
    """Search China court judgments by fuzzy title or case number.

    Coverage disclosure: this is a STATIC SNAPSHOT — incremental crawling is
    suspended and the corpus has known historical gaps (including 2011–2023);
    it is not the full national corpus. Ultra-broad single-character queries
    may exceed the query budget — add a year/province/doc_type filter or a
    selective term. Filters match decoded text (document type, province) and
    a closed judgment-year range. Returns {results: [{doc_id, case_no,
    title, court, cause, province, city, case_type, doc_type, procedure,
    court_level, judgment_year, judgment_result, publish_date, has_body,
    brand}], count, truncated}; dictionary reference keys never appear.
    Fetch full text with wenshu_read. ``scope``: named retrieval scope —
    unknown scope errors explicitly; a scope excluding the wenshu source
    returns an explainable empty result; a scoped response names the scope
    in effect.
    """
    from fd_find_data_business_mcp.tools_wenshu import wenshu_search as _impl

    meta, gate = _scope_gate(scope, "wenshu")
    if gate is not None:
        return gate
    out = _strip(_impl(title_query, doc_type, province, start_year,
                       end_year, limit))
    if meta:
        out["scope"] = meta
        _scope_record(meta, out.get("count", 0))
    return out


@mcp.tool
def wenshu_read(doc_id: str) -> dict:
    """Read one court judgment's decoded metadata and, when captured, full text.

    Returns metadata + {body_shape, content_available, content?, brand}.
    Judgments ingested metadata-only (body_shape=none) carry
    ``content_available: false`` honestly; a temporarily unreachable body
    store degrades to metadata with a note, never an error. An unknown id
    returns {"status": "not_found"}.
    """
    from fd_find_data_business_mcp.tools_wenshu import wenshu_read as _impl

    return _strip(_impl(doc_id))


@mcp.tool
def ia35_company_search(
    name_prefix: str | None = None,
    credit_code: str | None = None,
    limit: int = 20,
    scope: str | None = None,
) -> dict:
    """Search China business-registry firm profiles by name prefix or exact credit code.

    Corpus caliber: ia35 is a STATIC SNAPSHOT accumulated as yearly batches
    (the source column IS the batch year). Credit-code lookup serves ALL
    batches and returns the latest batch's row (the firm's current profile);
    name-prefix search serves the LATEST SNAPSHOT YEAR only (default 2023,
    env FDBIZ_IA35_SNAPSHOT_YEAR) — firms absent from that batch are
    reachable by code but not by name. Profiles carry decoded business
    scope / address / former names; dictionary reference keys never appear.
    Returns {results: [{credit_code, name, legal_rep, reg_capital,
    paid_capital, est_date, est_year, status, company_type, registrar,
    province, city, county, address, business_scope, former_names, tags,
    batch_year, brand}], count, truncated, snapshot_year?}. ``scope``: named
    retrieval scope — unknown scope errors explicitly; a scope excluding the
    ia35 source returns an explainable empty result; a scoped response names
    the scope in effect.
    """
    from fd_find_data_business_mcp.tools_firms import (
        ia35_company_search as _impl)

    meta, gate = _scope_gate(scope, "ia35")
    if gate is not None:
        return gate
    out = _strip(_impl(name_prefix, credit_code, limit))
    if meta:
        out["scope"] = meta
        _scope_record(meta, out.get("count", 0))
    return out


@mcp.tool
def ia35_reg_counts_read(
    start_year: int | None = None,
    end_year: int | None = None,
    province: str | None = None,
    industry: str | None = None,
    limit: int = 500,
) -> dict:
    """Read new-firm registration counts by year range, province, industry.

    Serves the reg_counts aggregate (founded-year x region x industry
    section -> new firm count). Source years mix true years with
    spreadsheet serial dates; serials are normalized to calendar years
    (e.g. 32741 -> 1989) and unconvertible rows are dropped with the count
    disclosed in the response note — a five-digit pseudo-year is never
    returned. Cells with no aggregate row are absent, never zero-filled.
    Province+year filters use the (province, year) index; province-only
    queries may exceed the query budget. Returns {results: [{year,
    province, city, county, industry, industry_code, new_firms, brand}],
    count, truncated, note?}.
    """
    from fd_find_data_business_mcp.tools_firms import (
        ia35_reg_counts_read as _impl)

    return _strip(_impl(start_year, end_year, province, industry, limit))


@mcp.tool
def wb_search_indicators(query: str, limit: int = 50,
                         scope: str | None = None) -> dict:
    """Search World Bank (WDI) indicators by fuzzy name, topic, or code.

    Returns {results: [{code, name, name_en, topic, unit, native_code,
    semantic_code, brand}], count, truncated}. Chinese and English names
    both work. native_code repeats the registry anchor (the WDI code) and
    semantic_code carries the unified indicator-registry identity — null
    when the entry is unverified or unregistered. If the domain store is
    unreachable, returns {"status": "domain_unavailable"} instead of failing.
    ``scope``: named retrieval scope — unknown scope errors explicitly; a
    scope excluding the world_bank source returns an explainable empty
    result; a scoped response names the scope in effect.
    """
    from fd_find_data_business_mcp.tools_wb import (
        wb_search_indicators as _impl)

    meta, gate = _scope_gate(scope, "wb")
    if gate is not None:
        return gate
    out = _strip(_impl(query, limit))
    if meta:
        out["scope"] = meta
        _scope_record(meta, out.get("count", 0))
    return out


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
def gta_search_variables(query: str, limit: int = 50,
                         scope: str | None = None) -> dict:
    """Search GTA listed-company panel variables by fuzzy Chinese label or code.

    Returns {results: [{code, label, native_code, semantic_code, brand}],
    count, truncated} — storage identity (table, sub-library) is not
    exposed. native_code repeats the registry anchor (the variable name;
    GTA semantic codes are its UPPERCASED form) and semantic_code carries
    the unified indicator-registry identity — null when the entry is
    unverified or unregistered. If the domain store is unreachable, returns
    {"status": "domain_unavailable"}. ``scope``: named retrieval scope —
    unknown scope errors explicitly; a scope excluding the gta_panel source
    returns an explainable empty result; a scoped response names the scope
    in effect.
    """
    from fd_find_data_business_mcp.tools_gta import (
        gta_search_variables as _impl)

    meta, gate = _scope_gate(scope, "gta")
    if gate is not None:
        return gate
    out = _strip(_impl(query, limit))
    if meta:
        out["scope"] = meta
        _scope_record(meta, out.get("count", 0))
    return out


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
def city_search_variables(query: str, limit: int = 50,
                          scope: str | None = None) -> dict:
    """Search China city-panel variables by fuzzy Chinese name or code.

    Returns {results: [{code, label, unit, native_code, semantic_code,
    brand}], count, truncated}. native_code repeats the registry anchor
    (the var_id) and semantic_code carries the unified indicator-registry
    identity — null when the entry is unverified or unregistered. If
    the domain store is unreachable, returns {"status": "domain_unavailable"}.
    ``scope``: named retrieval scope — unknown scope errors explicitly; a
    scope excluding the china_city_panel source returns an explainable
    empty result; a scoped response names the scope in effect.
    """
    from fd_find_data_business_mcp.tools_city import (
        city_search_variables as _impl)

    meta, gate = _scope_gate(scope, "city")
    if gate is not None:
        return gate
    out = _strip(_impl(query, limit))
    if meta:
        out["scope"] = meta
        _scope_record(meta, out.get("count", 0))
    return out


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


@mcp.tool
def registry_coverage() -> dict:
    """Indicator-registry coverage per source (registration boundary).

    Returns {results: [{source_db, label_zh, phase, registered, verified,
    unverified, actual_in_source, coverage}], count, census_as_of}: live
    registered/verified counts from the unified indicator registry
    (fd_open_data.registry_entries) for the five registered sources
    (world_bank, yearbook_catalog, gta_panel, china_city_panel,
    fd_open_data mirror), plus the phase-2 unregistered sources (ia35
    工商注册, wenshu_db 裁判文书) and law_db, which is document-domain
    caliber (法条/切片/指导案例 are documents, not indicators — 0 entries
    is expected). coverage = registered / actual_in_source against the
    census anchors. Fail-soft: if the registry is unreachable the static
    declarations are served with a note.
    """
    from fd_find_data_business_mcp.tools_registry import registry_coverage as _impl

    return _strip(_impl())


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
