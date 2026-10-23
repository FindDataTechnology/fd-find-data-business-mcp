"""MCP tool behavior annotations (mcp-tool-annotations).

One table, one entry per registered tool. ``server.py`` wires every
``@mcp.tool`` with ``annotations=annotations_for("<name>")`` and
``tests/test_tool_annotations.py`` compares this table against the runtime
registry — a new tool without an entry fails loudly instead of shipping with
no hints.

Judgment rules (from the change spec):

- ``readOnlyHint``: the call changes no persistent state.
- ``destructiveHint``: only irreversible deletion/overwrite. Every writer in
  this server is an upsert or a counter increment, so all are False.
- ``idempotentHint``: same input repeated produces the same result AND state.
- ``openWorldHint``: True only when the result depends on a non-enumerable,
  changing external set (an upstream crawl, a live external API, an LLM).
  Reading a versioned database — including the federated domain stores and
  the RustFS body blocks — is False: connecting to a database does not make
  a tool open-world.

Two families need a note because their names suggest "query" while the
implementation writes:

- The fourteen scoped domain search/list tools increment per-(scope, day)
  hit counters (``server._scope_record`` -> ``scoping.record_hit``) on a
  permitted scoped call. That is a persistent state write, so they are not
  read-only; the cumulative counters also make replay non-idempotent.
- ``read_range`` fills a cold or stale range from an upstream provider and
  upserts the observations into the cache. The upsert is keyed on
  (concept, entity, date, source), so replay converges to the same rows
  (idempotent, non-destructive), but the upstream fetch makes it
  open-world.

``wenshu_read`` fetches the body block over the network from RustFS, but that
is this service's own versioned object store: a given doc id always decodes to
the same text, so it is not open-world.
"""
from __future__ import annotations

from typing import Any

#: Query/read only: no persistent state change.
READ_ONLY: dict[str, Any] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}

#: Domain search/list tools: read the federated store, but a permitted scoped
#: call increments the per-(scope, day) counters in ``scope_stats``. A counter
#: increment is a write, and replay accumulates more of it -> not idempotent.
METERED_SEARCH: dict[str, Any] = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}

#: ``read_range``: cache-filling upsert backed by a live upstream fetch.
#: Natural-key upsert -> replay reproduces the same rows; upstream -> open.
CACHE_FILL: dict[str, Any] = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}

#: Tool name -> four hints. Every registered tool must appear here; the test
#: asserts exact set equality with the runtime registry.
TOOL_ANNOTATIONS: dict[str, dict[str, Any]] = {
    # Core reads (own crawled store / local embedding model / graph cache).
    "read": dict(READ_ONLY),
    "read_range": dict(CACHE_FILL),
    "indicator_series_read": dict(CACHE_FILL),  # cold ranges upsert via read_range
    "list_concepts": dict(READ_ONLY),
    "ai_search": dict(READ_ONLY),
    "graph_search": dict(READ_ONLY),
    # Domain federation: search/list tools meter scoped calls; read tools do not.
    "yearbook_search_indicators": dict(METERED_SEARCH),
    "yearbook_read": dict(READ_ONLY),
    "yearbook_regions": dict(READ_ONLY),
    "law_search": dict(METERED_SEARCH),
    "law_read": dict(READ_ONLY),
    "legal_docs_search": dict(METERED_SEARCH),
    "legal_docs_read": dict(READ_ONLY),
    "guide_cases_search": dict(METERED_SEARCH),
    "guide_cases_read": dict(READ_ONLY),
    "wenshu_search": dict(METERED_SEARCH),
    "wenshu_read": dict(READ_ONLY),
    "ia35_company_search": dict(METERED_SEARCH),
    "ia35_reg_counts_read": dict(READ_ONLY),
    "wb_search_indicators": dict(METERED_SEARCH),
    "wb_read": dict(READ_ONLY),
    "gta_search_variables": dict(METERED_SEARCH),
    "gta_read": dict(READ_ONLY),
    "city_search_variables": dict(METERED_SEARCH),
    "city_read": dict(READ_ONLY),
    # DAAS catalog: same split.
    "daas_list_sources": dict(METERED_SEARCH),
    "daas_search_functions": dict(METERED_SEARCH),
    "daas_get_function_detail": dict(READ_ONLY),
    "daas_list_indicators": dict(METERED_SEARCH),
    "daas_get_indicator": dict(READ_ONLY),
    "daas_list_rules": dict(METERED_SEARCH),
    "daas_search_entities": dict(METERED_SEARCH),
    "daas_get_entity": dict(READ_ONLY),
    "registry_coverage": dict(READ_ONLY),
}


def annotations_for(name: str) -> dict[str, Any]:
    """Return a copy of the tool's annotations; unknown names raise KeyError.

    A silent default would let an unregistered tool pass with hints that
    happen to look right — the failure mode this table exists to prevent.
    """
    try:
        return dict(TOOL_ANNOTATIONS[name])
    except KeyError:
        raise KeyError(
            f"tool {name!r} has no entry in annotations.TOOL_ANNOTATIONS"
        ) from None
