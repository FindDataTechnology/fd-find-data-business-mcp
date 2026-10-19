"""fd-find-data-business-mcp — commercial, login-gated FindData MCP server.

Thin shell over fd-open-data-mcp: reuses its cache, dispatch, ontology, and
search as a library, but exposes zero upstream-provider identity. Every served
value is branded ``finddata``. Auth is Logto JWT via FastMCP's ``JWTVerifier``.
"""
from __future__ import annotations

import os
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from fd_open_data_mcp.db import get_database

from fd_find_data_business_mcp.annotations import annotations_for

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


def _count_read_rows(tool_name: str, structured: Any) -> int | None:
    """Derive ``rows_returned`` from a bare-payload read tool's wire shape.

    ``read`` serves its bare list wrapped by fastmcp as ``{"result": [rows]}``
    (the framework's pre-existing convention for non-object outputs);
    ``unavailable`` placeholder rows are not observation rows and don't count.
    ``read_range`` serves the bare ``{concept_id: [rows]}`` map. Any other
    shape returns None — the result is then left unstamped, so a missing
    ``rows_returned`` raises a billing-side alert instead of a wrong number.
    """
    if tool_name == "read":
        rows = structured.get("result") if isinstance(structured, dict) else None
        if isinstance(rows, list):
            return sum(1 for r in rows
                       if not (isinstance(r, dict)
                               and r.get("status") == "unavailable"))
    elif tool_name == "read_range":
        if isinstance(structured, dict):
            return sum(len(v) for v in structured.values()
                       if isinstance(v, list))
    return None


class _RowsReturnedMiddleware(Middleware):
    """Metering stamp for the bare-payload read tools (wire-platform-v1 1.1).

    ``read``/``read_range`` keep their original bare wire payloads (list /
    ``{concept_id: rows}``) — no envelope field is added, so mirror clients
    that parse the bare structure are unaffected. ``rows_returned`` rides the
    protocol-standard result ``_meta`` instead, computed from the served
    payload after the tool ran. Existing ``_meta`` keys (fastmcp's
    wrap-result marker) are preserved, never replaced. The in-envelope
    ``rows_returned`` of the five domain read tools is stamped separately in
    their wrappers (they were envelope-shaped from the start).
    """

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext,
    ):
        result = await call_next(context)
        name = context.message.name
        if name in ("read", "read_range"):
            count = _count_read_rows(name, result.structured_content)
            if count is not None:
                meta = dict(result.meta or {})
                meta["rows_returned"] = count
                result.meta = meta
        return result


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
    middleware=[_RowsReturnedMiddleware()],
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


def _with_rows_returned(out: dict) -> dict:
    """Stamp the platform metering contract onto a read tool's envelope.

    ``rows_returned`` (int) = observation rows actually served in this
    response — the wire platform's per-row billing anchor (change:
    wire-platform-v1). Envelopes without a ``results`` list (fail-soft
    ``domain_unavailable``, ``unknown_variable``, scope-gate errors) are not
    successes and pass through untouched. Additive only: every existing
    field is preserved.
    """
    if isinstance(out, dict) and isinstance(out.get("results"), list):
        out["rows_returned"] = len(out["results"])
    return out


# --- Read: serve crawled cache only (fresh OR stale); provider-independent ----
@mcp.tool(annotations=annotations_for("read"))
def read(concept_id: int, entity_type: str, entity_id: int, dates: list[str]) -> list[dict]:
    """Read a FindData indicator for an entity over a list of dates.

    Serves any cached observation (fresh or stale) — stability over recency, and
    zero runtime dependency on upstream providers: the read path never
    dispatches to akshare/wbgapi/etc.; it reads only FindData's crawled store.
    A never-crawled date returns ``unavailable``. No value is fabricated.
    The bare list payload is unchanged; the observation-row count rides the
    MCP result ``_meta`` as ``rows_returned`` (unavailable placeholders not
    counted).
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
@mcp.tool(annotations=annotations_for("read_range"))
def read_range(
    concept_ids: list[int], entity_type: str, entity_id: int,
    start: str, end: str,
) -> dict[int, list[dict]]:
    """Bulk-read one or more indicators for an entity over ``[start, end]``.

    Cached rows (fresh or stale) are served; a cold range (no rows) is fetched
    synchronously and recorded. Returns ``{concept_id: [{date, value, unit,
    brand: "finddata"}, ...]}``; concepts with no data get an empty list.
    The bare map payload is unchanged; the total observation-row count rides
    the MCP result ``_meta`` as ``rows_returned``.
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
@mcp.tool(annotations=annotations_for("list_concepts"))
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


@mcp.tool(annotations=annotations_for("ai_search"))
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


@mcp.tool(annotations=annotations_for("graph_search"))
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
    "daas": ["daas"],
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


@mcp.tool(annotations=annotations_for("yearbook_search_indicators"))
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


@mcp.tool(annotations=annotations_for("yearbook_read"))
def yearbook_read(
    indicator_id: int,
    region: str | None = None,
    start_year: int | None = None,
    end_year: int | None = None,
    limit: int = 200,
) -> dict:
    """Read a yearbook indicator's annual series, optionally for one region
    and year range.

    Returns {results: [{year, region, value, unit, brand}], count, truncated,
    rows_returned}; a filter with no data returns an empty results list
    (truthful, not an error). Latest edition wins when multiple yearbook
    editions cover the same region-year. ``rows_returned`` = observation rows
    served (equal to count).
    """
    from fd_find_data_business_mcp.tools_yearbook import yearbook_read as _impl

    return _with_rows_returned(_strip(_impl(indicator_id, region, start_year, end_year, limit)))


@mcp.tool(annotations=annotations_for("law_search"))
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


@mcp.tool(annotations=annotations_for("law_read"))
def law_read(law_id: int) -> dict:
    """Read one law's full text and metadata by id (from law_search).

    Returns {id, title, category, type, status, publish, expiry, content,
    brand}; an unknown id returns {"status": "not_found"}.
    """
    from fd_find_data_business_mcp.tools_law import law_read as _impl

    return _strip(_impl(law_id))


@mcp.tool(annotations=annotations_for("legal_docs_search"))
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


@mcp.tool(annotations=annotations_for("legal_docs_read"))
def legal_docs_read(doc_id: int, offset: int = 0, max_chars: int = 12000) -> dict:
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

    return _strip(_impl(doc_id, offset=offset, max_chars=max_chars))


@mcp.tool(annotations=annotations_for("guide_cases_search"))
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


@mcp.tool(annotations=annotations_for("guide_cases_read"))
def guide_cases_read(case_id: str, offset: int = 0, max_chars: int = 12000) -> dict:
    """Read one guiding case or notice by source_id (from guide_cases_search).

    Returns full metadata + the published structured sections
    (keyword/gist/facts/result/reasoning/statutes — unpublished sections are
    absent, never fabricated) + ``body`` full text + ``reference_status``
    with ``reference_note`` (the notice number that declared 不再参照).
    An unknown id returns {"status": "not_found"}.
    """
    from fd_find_data_business_mcp.tools_guide_cases import (
        guide_cases_read as _impl)

    return _strip(_impl(case_id, offset=offset, max_chars=max_chars))


@mcp.tool(annotations=annotations_for("wenshu_search"))
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


@mcp.tool(annotations=annotations_for("wenshu_read"))
def wenshu_read(doc_id: str, offset: int = 0, max_chars: int = 12000) -> dict:
    """Read one court judgment's decoded metadata and, when captured, full text.

    Returns metadata + {body_shape, content_available, content?, brand}.
    Judgments ingested metadata-only (body_shape=none) carry
    ``content_available: false`` honestly; a temporarily unreachable body
    store degrades to metadata with a note, never an error. An unknown id
    returns {"status": "not_found"}.
    """
    from fd_find_data_business_mcp.tools_wenshu import wenshu_read as _impl

    return _strip(_impl(doc_id, offset=offset, max_chars=max_chars))


@mcp.tool(annotations=annotations_for("ia35_company_search"))
def ia35_company_search(
    name_prefix: str | None = None,
    credit_code: str | None = None,
    limit: int = 20,
    scope: str | None = None,
    company_type: str | None = None,
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
    batch_year, brand}], count, truncated, snapshot_year?,
    snapshot_caliber?}. ``company_type`` filters on the decoded 企业类型
    text (e.g. "个体工商户" — roughly half the corpus) and is echoed back so
    an empty result is attributable to the filter. The corpus is a yearly
    batch snapshot, NOT live registry state: responses carry
    ``snapshot_year`` / ``snapshot_caliber`` and MUST NOT be presented as
    current registration status. ``scope``: named retrieval scope — unknown
    scope errors explicitly; a scope excluding the ia35 source returns an
    explainable empty result; a scoped response names the scope in effect.
    """
    from fd_find_data_business_mcp.tools_firms import (
        ia35_company_search as _impl)

    meta, gate = _scope_gate(scope, "ia35")
    if gate is not None:
        return gate
    out = _strip(_impl(name_prefix, credit_code, limit, company_type))
    if meta:
        out["scope"] = meta
        _scope_record(meta, out.get("count", 0))
    return out


@mcp.tool(annotations=annotations_for("ia35_reg_counts_read"))
def ia35_reg_counts_read(
    start_year: int | None = None,
    end_year: int | None = None,
    province: str | None = None,
    industry: str | None = None,
    limit: int = 500,
    city: str | None = None,
    county: str | None = None,
    industry_class: str | None = None,
    industry_level: str = "门类",
) -> dict:
    """Read new-firm registration counts by year, region, and industry level.

    Serves the reg_counts aggregate (founded-year x region x industry
    section -> new firm count). Source years mix true years with
    spreadsheet serial dates; serials are normalized to calendar years
    (e.g. 32741 -> 1989) and unconvertible rows are dropped with the count
    disclosed in the response note — a five-digit pseudo-year is never
    returned. Cells with no aggregate row are absent, never zero-filled.
    Province+year filters use the (province, year) index.

    ``city`` / ``county`` narrow the region and REQUIRE ``province``
    (without it the query degenerates into a 12.4M-row scan that exceeds
    the federated timeout — the tool returns invalid_params instead).
    ``industry_class`` matches at the level named by ``industry_level``
    (门类/大类/中类/小类, default 门类) against that level's code or name,
    e.g. industry_level="小类" with "快餐服务" or "H6220".

    This aggregate counts NEW REGISTRATIONS only — there is no closure,
    deregistration, or revocation series in this source, so it MUST NOT be
    used to answer "how many closed". Responses carry
    ``latest_year_available`` and ``snapshot_caliber``: the corpus is a
    year-batch snapshot, not live registry state. Returns {results:
    [{year, province, city, county, industry, industry_code, new_firms,
    brand}], count, truncated, rows_returned, latest_year_available?,
    snapshot_caliber?, note?}. ``rows_returned`` = aggregate observation
    rows served (equal to count).
    """
    from fd_find_data_business_mcp.tools_firms import (
        ia35_reg_counts_read as _impl)

    return _with_rows_returned(_strip(_impl(
        start_year, end_year, province, industry, limit,
        city, county, industry_class, industry_level)))


@mcp.tool(annotations=annotations_for("wb_search_indicators"))
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


@mcp.tool(annotations=annotations_for("wb_read"))
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
    truncated, rows_returned}; missing country-year cells are absent
    (truthful, never fabricated); an unknown indicator or country returns an
    empty list. ``rows_returned`` = observation rows served (equal to count).
    """
    from fd_find_data_business_mcp.tools_wb import wb_read as _impl

    return _with_rows_returned(_strip(_impl(indicator_code, countries, start_year, end_year, limit)))


@mcp.tool(annotations=annotations_for("gta_search_variables"))
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


@mcp.tool(annotations=annotations_for("gta_read"))
def gta_read(
    variable: str,
    start_year: int | None = None,
    end_year: int | None = None,
    firm_ids: list[int] | None = None,
    limit: int = 200,
) -> dict:
    """Read one GTA panel variable's firm-year values.

    Returns {results: [{firm_id, firm, year, value, brand}], count,
    truncated, rows_returned}, ordered by year then firm; an unknown
    variable returns {"status": "unknown_variable"}; an empty year range is
    a truthful empty list. Use gta_search_variables first to find variable
    codes. ``rows_returned`` = observation rows served (equal to count).
    """
    from fd_find_data_business_mcp.tools_gta import gta_read as _impl

    return _with_rows_returned(_strip(_impl(variable, start_year, end_year, firm_ids, limit)))


@mcp.tool(annotations=annotations_for("city_search_variables"))
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


@mcp.tool(annotations=annotations_for("city_read"))
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
    truncated, rows_returned}; an unknown variable returns
    {"status": "unknown_variable"}; a filter with no data is a truthful
    empty list. ``rows_returned`` = observation rows served (equal to count).
    """
    from fd_find_data_business_mcp.tools_city import city_read as _impl

    return _with_rows_returned(_strip(_impl(variable, city, start_year, end_year, limit)))


# --- DAAS supply domain (facet-mcp-foundation-v1) ----------------------------
# Read-only catalog/caliber/reference federation over the data machine's daas
# database (sources / daas_functions / indicator_rules / rules mirrors). Same
# fail-soft discipline as every domain above; observation values are outside
# v1. Tool names carry the ``daas_`` prefix (distinct caliber from the
# indicator registry's macro catalogs) and every exposure is bounded by the
# DAAS tool-surface manifest's commercial set (asserted in tests).

@mcp.tool(annotations=annotations_for("daas_list_sources"))
def daas_list_sources(
    keyword: str | None = None,
    enabled: bool | None = None,
    limit: int = 100,
    scope: str | None = None,
) -> dict:
    """List FindData's DAAS supply catalog of data sources.

    Each row: {id, name, label, description, url, enabled, brand}. ``keyword``
    substring-matches name/label/description; ``enabled`` filters by state.
    Source configuration (``config``) is never exposed. Returns
    {results, count, truncated, rows_returned}; ``rows_returned`` = catalog
    rows served (equal to count). If the daas store is unreachable, returns
    {"status": "domain_unavailable"}. ``scope``: named retrieval scope — an
    unknown scope errors explicitly; a scope excluding the daas source
    returns an explainable empty result; a scoped response names the scope
    in effect.
    """
    from fd_find_data_business_mcp.tools_daas import daas_list_sources as _impl

    meta, gate = _scope_gate(scope, "daas")
    if gate is not None:
        return gate
    out = _with_rows_returned(_strip(_impl(keyword, enabled, limit)))
    if meta:
        out["scope"] = meta
        _scope_record(meta, out.get("count", 0))
    return out


@mcp.tool(annotations=annotations_for("daas_search_functions"))
def daas_search_functions(
    keyword: str,
    category: str | None = None,
    source_id: int | None = None,
    limit: int = 50,
    scope: str | None = None,
) -> dict:
    """Search FindData DAAS fetch functions by fuzzy name/label/description.

    Ranked exact > prefix > contains; ``category`` filters exactly (e.g.
    Reference); ``source_id`` pins one catalog source. Returns {results:
    [{id, source_id, name, label, description, category, output_type,
    frequency, brand}], count, truncated, rows_returned} — the request-shape
    ``parameters`` caliber rides only with daas_get_function_detail.
    ``rows_returned`` = catalog rows served (equal to count).
    ``scope``: named retrieval scope — unknown scope errors explicitly; a
    scope excluding the daas source returns an explainable empty result; a
    scoped response names the scope in effect.
    """
    from fd_find_data_business_mcp.tools_daas import daas_search_functions as _impl

    meta, gate = _scope_gate(scope, "daas")
    if gate is not None:
        return gate
    out = _with_rows_returned(_strip(_impl(keyword, category, source_id, limit)))
    if meta:
        out["scope"] = meta
        _scope_record(meta, out.get("count", 0))
    return out


@mcp.tool(annotations=annotations_for("daas_get_function_detail"))
def daas_get_function_detail(function_id: int) -> dict:
    """Read one DAAS fetch function's full caliber by id (from search).

    Adds ``parameters`` — the function's request shape (path, method, query
    params, gating) — plus output_type and frequency. An unknown id returns
    {"status": "not_found"}; an unreachable daas store returns
    {"status": "domain_unavailable"}.
    """
    from fd_find_data_business_mcp.tools_daas import (
        daas_get_function_detail as _impl)

    return _strip(_impl(function_id))


@mcp.tool(annotations=annotations_for("daas_list_indicators"))
def daas_list_indicators(
    keyword: str | None = None,
    datasource: str | None = None,
    enabled: bool | None = None,
    limit: int = 100,
    scope: str | None = None,
) -> dict:
    """List FindData's DAAS computed-indicator rule catalog.

    Each row is one indicator rule's caliber: datasource function, source
    table/columns, operator (op, e.g. sma). ``keyword`` substring-matches
    name/indicator_name; ``datasource`` filters exactly; ``enabled`` by rule
    state. Returns {results, count, truncated, rows_returned};
    ``rows_returned`` = catalog rows served (equal to count). These computed
    indicators are a separate caliber from the macro indicator registry
    (see registry_coverage). If the daas store is unreachable, returns
    {"status": "domain_unavailable"}. ``scope``: named retrieval scope — an
    unknown scope errors explicitly; a scope excluding the daas source
    returns an explainable empty result; a scoped response names the scope
    in effect.
    """
    from fd_find_data_business_mcp.tools_daas import daas_list_indicators as _impl

    meta, gate = _scope_gate(scope, "daas")
    if gate is not None:
        return gate
    out = _with_rows_returned(_strip(_impl(keyword, datasource, enabled, limit)))
    if meta:
        out["scope"] = meta
        _scope_record(meta, out.get("count", 0))
    return out


@mcp.tool(annotations=annotations_for("daas_get_indicator"))
def daas_get_indicator(indicator_id: int) -> dict:
    """Read one DAAS indicator rule's complete caliber by id (from list).

    Adds ``params_json`` — the operator arguments (e.g. {"window": 5} for an
    sma rule) — to the list-row projection. An unknown id returns
    {"status": "not_found"}; an unreachable daas store returns
    {"status": "domain_unavailable"}.
    """
    from fd_find_data_business_mcp.tools_daas import daas_get_indicator as _impl

    return _strip(_impl(indicator_id))


@mcp.tool(annotations=annotations_for("daas_list_rules"))
def daas_list_rules(
    rule_type: str | None = None,
    limit: int = 100,
    scope: str | None = None,
) -> dict:
    """List FindData's DAAS unified rule catalog.

    Each row: {id, name, rule_type, target, config_json, description,
    enabled, brand}; ``config_json`` is the rule's caliber. ``rule_type``
    filters exactly. Returns {results, count, truncated, rows_returned};
    ``rows_returned`` = catalog rows served (equal to count). If the daas
    store is unreachable, returns {"status": "domain_unavailable"}.
    ``scope``: named retrieval scope — an unknown scope errors explicitly;
    a scope excluding the daas source returns an explainable empty result;
    a scoped response names the scope in effect.
    """
    from fd_find_data_business_mcp.tools_daas import daas_list_rules as _impl

    meta, gate = _scope_gate(scope, "daas")
    if gate is not None:
        return gate
    out = _with_rows_returned(_strip(_impl(rule_type, limit)))
    if meta:
        out["scope"] = meta
        _scope_record(meta, out.get("count", 0))
    return out


@mcp.tool(annotations=annotations_for("daas_search_entities"))
def daas_search_entities(
    keyword: str,
    entity_type: str | None = None,
    limit: int = 50,
    scope: str | None = None,
) -> dict:
    """Search FindData DAAS entities (stocks + countries) by name/ticker/code.

    DATA READINESS: the entities mirror table is not yet provisioned in the
    daas store (the entity master is fd-open-data-mcp; the v1 sync scope
    covers the 4 catalog tables only), so until it lands every call returns
    {"status": "domain_unavailable"} — the tool is registered but its data
    is pending, which is a legal state. Once provisioned, returns {results:
    [{id, entity_type, code, name, ticker, exchange, country_code,
    description, brand}], count, truncated, rows_returned}; ``entity_type``
    filters exactly (stock / country). ``scope``: named retrieval scope —
    an unknown scope errors explicitly; a scope excluding the daas source
    returns an explainable empty result; a scoped response names the scope
    in effect.
    """
    from fd_find_data_business_mcp.tools_daas import daas_search_entities as _impl

    meta, gate = _scope_gate(scope, "daas")
    if gate is not None:
        return gate
    out = _with_rows_returned(_strip(_impl(keyword, entity_type, limit)))
    if meta:
        out["scope"] = meta
        _scope_record(meta, out.get("count", 0))
    return out


@mcp.tool(annotations=annotations_for("daas_get_entity"))
def daas_get_entity(entity_id: int) -> dict:
    """Read one DAAS entity's detail by id (from search).

    DATA READINESS: as with daas_search_entities — the entities mirror table
    is not yet provisioned in the daas store, so calls return
    {"status": "domain_unavailable"} until it lands. Once provisioned: the
    full entity row; an unknown id returns {"status": "not_found"}.
    """
    from fd_find_data_business_mcp.tools_daas import daas_get_entity as _impl

    return _strip(_impl(entity_id))


@mcp.tool(annotations=annotations_for("registry_coverage"))
def registry_coverage() -> dict:
    """Indicator-registry coverage per source (registration boundary + calibers).

    Returns {results: [{source_db, label_zh, phase, registered, verified,
    unverified, actual_in_source, coverage}], count, census_as_of, calibers}:
    live registered/verified counts from the unified indicator registry
    (fd_open_data.registry_entries) for the five registered sources
    (world_bank, yearbook_catalog, gta_panel, china_city_panel,
    fd_open_data mirror), plus the phase-2 unregistered sources (ia35
    工商注册, wenshu_db 裁判文书) and law_db, which is document-domain
    caliber (法条/切片/指导案例 are documents, not indicators — 0 entries
    is expected). coverage = registered / actual_in_source against the
    census anchors. ``calibers`` is the authoritative indicator-count
    aggregation (登记指标 registered_total / verified 总量 verified_total /
    可服务指标 servable_total), each with a composition note — presentation
    surfaces take their numbers from here, never hand-written. Fail-soft:
    if the registry is unreachable the static declarations are served with
    a note and caliber totals are null.
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
