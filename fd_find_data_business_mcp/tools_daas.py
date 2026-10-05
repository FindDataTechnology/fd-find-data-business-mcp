"""DAAS supply-domain tools over the federated ``daas`` database (finddata-branded).

The daas store on the data-machine PG (same host as the other federated
domains, read-only role ``fdbiz_ro``) mirrors the DAAS workspace's catalog
tables — ``sources`` (21) / ``daas_functions`` (57) / ``indicator_rules``
(391) / ``rules`` (1) as of the 2026-10-06 bootstrap — via
fd-daas-mcp's ``sync_postgres`` (full-replace per run, every row stamped
``synced_at``). This is the supply-side caliber surface: directory /
caliber / reference metadata only. Observation values are deliberately
outside v1 (facet-mcp-foundation-v1 design D7), and nothing here can write
workspace state — every tool is a read.

Discipline mirrors the law domain exactly: per-tool field whitelists
(``_*_FIELDS``) decide what leaves this module, and ``_FORBIDDEN_KEYS`` is
the belt-and-braces drop of provenance/bookkeeping columns — ``config``
(sources' crawler/portal config; the credential carrier), ``category_id``
(internal FK to the unsynced categories table), ``score`` (internal
curation bookkeeping) and ``created_at`` / ``updated_at`` / ``synced_at``
(sync bookkeeping). Every served row is branded ``brand: "finddata"``.
Any connection failure, timeout, or schema drift passes through fail-soft
as ``{"status": "domain_unavailable", ...}`` — never a raise.

``entities`` readiness: the entity master is fd-open-data-mcp (DAAS design
D5) and the v1 sync whitelist is the 4 catalog tables above, so the daas
store has NO ``entities`` table yet. ``daas_search_entities`` /
``daas_get_entity`` are implemented against ``entities`` with the natural
key shape DAAS re-keyed to (``entity_type``, ``code``); until the mirror
table is provisioned every call fails soft to ``domain_unavailable`` — a
registered tool without its data is a legal state, and when the table
lands (DEFAULT PRIVILEGES already grant fdbiz_ro SELECT) the tools start
serving with zero code change. The provisional column projection below is
the planned mirror shape and is asserted against real rows only by the
integration test once data exists.
"""
from __future__ import annotations

from typing import Any

from fd_find_data_business_mcp.domain import query as domain_query

_DB = "daas"

_BRAND = "finddata"

#: Provenance / bookkeeping columns that never leave this module, even if a
#: future SELECT drifts into them. ``config`` may carry portal credentials;
#: the rest is sync/curation bookkeeping or internal links.
_FORBIDDEN_KEYS = frozenset({
    "config", "category_id", "score",
    "created_at", "updated_at", "synced_at",
})

_MAX_LIMIT = 200

_SOURCE_FIELDS = ("id", "name", "label", "description", "url", "enabled")

_FUNCTION_FIELDS = ("id", "source_id", "name", "label", "description",
                    "category", "output_type", "frequency")
_FUNCTION_DETAIL_FIELDS = _FUNCTION_FIELDS + ("parameters",)

#: ``datasource`` / ``source_table`` are caliber columns (which store the
#: rule reads), not provenance — the supply catalog is itself the product.
_INDICATOR_FIELDS = ("id", "name", "indicator_name", "datasource",
                     "function_name", "source_table", "date_column",
                     "value_column", "op", "enabled")
_INDICATOR_DETAIL_FIELDS = _INDICATOR_FIELDS + ("params_json",)

_RULE_FIELDS = ("id", "name", "rule_type", "target", "config_json",
                "description", "enabled")

#: Provisional entities mirror shape (DAAS natural key (entity_type, code);
#: see module docstring for the readiness condition).
_ENTITY_FIELDS = ("id", "entity_type", "code", "name", "ticker", "exchange",
                  "country_code", "description")


def _escape_like(s: str) -> str:
    """Neutralize LIKE wildcards in user input (backslash is the default escape)."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _clamp(limit: int) -> int:
    return max(1, min(int(limit), _MAX_LIMIT))


def _project(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    """Whitelist-projection + brand, minus any forbidden provenance key."""
    out = {k: row.get(k) for k in fields}
    out["brand"] = _BRAND
    return {k: v for k, v in out.items() if k not in _FORBIDDEN_KEYS}


def _envelope(rows: list[dict[str, Any]], fields: tuple[str, ...],
              limit: int) -> dict[str, Any]:
    truncated = len(rows) > limit
    results = [_project(r, fields) for r in rows[:limit]]
    return {"results": results, "count": len(results), "truncated": truncated}


def _fail(rows: Any) -> bool:
    """True when the domain layer answered a fail-soft dict (pass through)."""
    return isinstance(rows, dict)


def daas_list_sources(
    keyword: str | None = None,
    enabled: bool | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """List the DAAS supply catalog's data sources, optionally filtered.

    ``keyword`` substring-matches name / label / description; ``enabled``
    filters by the source's on/off state. Returns ``{results: [{id, name,
    label, description, url, enabled, brand}], count, truncated}`` ordered
    by id. The per-source ``config`` (crawler/portal configuration, may
    carry credentials) never leaves this module. A domain failure is passed
    through unchanged as ``{"status": "domain_unavailable"}``.
    """
    where, params = [], {"lim": _clamp(limit) + 1}
    if keyword and keyword.strip():
        esc = _escape_like(keyword.strip())
        params["pat"] = f"%{esc}%"
        where.append("(name ILIKE :pat OR label ILIKE :pat"
                     " OR description ILIKE :pat)")
    if enabled is not None:
        params["enabled"] = bool(enabled)
        where.append("enabled = :enabled")
    sql = "SELECT id, name, label, description, url, enabled\nFROM sources"
    if where:
        sql += "\nWHERE " + "\n  AND ".join(where)
    sql += "\nORDER BY id\nLIMIT :lim"
    rows = domain_query(_DB, sql, params)
    if _fail(rows):
        return rows
    return _envelope(rows, _SOURCE_FIELDS, _clamp(limit))


def daas_search_functions(
    keyword: str,
    category: str | None = None,
    source_id: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Search DAAS fetch functions by fuzzy name / label / description.

    Ranked exact > prefix > contains on ``name``; ``category`` filters
    exactly (e.g. Reference, Earnings); ``source_id`` pins one catalog
    source (from daas_list_sources). Returns ``{results: [{id, source_id,
    name, label, description, category, output_type, frequency, brand}],
    count, truncated}`` — the ``parameters`` caliber rides only with
    daas_get_function_detail, never in search rows. A blank keyword yields
    a truthful empty envelope without querying; a domain failure passes
    through unchanged.
    """
    q = (keyword or "").strip()
    if not q:
        return {"results": [], "count": 0, "truncated": False}
    limit = _clamp(limit)
    esc = _escape_like(q)
    params: dict[str, Any] = {
        "pat": f"%{esc}%", "pfx": f"{esc}%", "q": q, "lim": limit + 1,
    }
    where = ["(name ILIKE :pat OR label ILIKE :pat OR description ILIKE :pat)"]
    if category:
        params["category"] = category.strip()
        where.append("category = :category")
    if source_id is not None:
        params["source_id"] = int(source_id)
        where.append("source_id = :source_id")
    sql = (
        "SELECT id, source_id, name, label, description, category,"
        " output_type, frequency\n"
        "FROM daas_functions\n"
        "WHERE " + "\n  AND ".join(where) + "\n"
        "ORDER BY (lower(name) = lower(:q)) DESC,\n"
        "         (name ILIKE :pfx) DESC,\n"
        "         position(lower(:q) IN lower(name)),\n"
        "         id\n"
        "LIMIT :lim"
    )
    rows = domain_query(_DB, sql, params)
    if _fail(rows):
        return rows
    return _envelope(rows, _FUNCTION_FIELDS, limit)


def daas_get_function_detail(function_id: int) -> dict[str, Any]:
    """Read one DAAS fetch function's full caliber by id (from search).

    Adds the ``parameters`` JSON — the function's request shape (path,
    method, query params, gating) — plus output_type and frequency. An
    unknown id returns ``{"status": "not_found"}``; a federation failure
    passes through as ``{"status": "domain_unavailable"}``.
    """
    rows = domain_query(
        _DB,
        "SELECT id, source_id, name, label, description, category,"
        " parameters, output_type, frequency\n"
        "FROM daas_functions\n"
        "WHERE id = :fid",
        {"fid": int(function_id)},
    )
    if _fail(rows):
        return rows
    if not rows:
        return {"status": "not_found", "brand": _BRAND}
    return _project(rows[0], _FUNCTION_DETAIL_FIELDS)


def daas_list_indicators(
    keyword: str | None = None,
    datasource: str | None = None,
    enabled: bool | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """List the DAAS computed-indicator rule catalog, optionally filtered.

    Each row is one indicator rule's caliber: which datasource function
    over which source table/columns, with which operator (``op``, e.g.
    ``sma``). ``keyword`` substring-matches name / indicator_name;
    ``datasource`` filters exactly; ``enabled`` by rule state. Returns
    ``{results: [{id, name, indicator_name, datasource, function_name,
    source_table, date_column, value_column, op, enabled, brand}], count,
    truncated}`` ordered by id — ``params_json`` (the operator arguments)
    rides only with daas_get_indicator. A domain failure passes through
    unchanged.
    """
    where, params = [], {"lim": _clamp(limit) + 1}
    if keyword and keyword.strip():
        esc = _escape_like(keyword.strip())
        params["pat"] = f"%{esc}%"
        where.append("(name ILIKE :pat OR indicator_name ILIKE :pat)")
    if datasource:
        params["datasource"] = datasource.strip()
        where.append("datasource = :datasource")
    if enabled is not None:
        params["enabled"] = bool(enabled)
        where.append("enabled = :enabled")
    sql = (
        "SELECT id, name, indicator_name, datasource, function_name,"
        " source_table, date_column, value_column, op, enabled\n"
        "FROM indicator_rules"
    )
    if where:
        sql += "\nWHERE " + "\n  AND ".join(where)
    sql += "\nORDER BY id\nLIMIT :lim"
    rows = domain_query(_DB, sql, params)
    if _fail(rows):
        return rows
    return _envelope(rows, _INDICATOR_FIELDS, _clamp(limit))


def daas_get_indicator(indicator_id: int) -> dict[str, Any]:
    """Read one DAAS indicator rule's complete caliber by id (from list).

    Adds ``params_json`` — the operator arguments (e.g. ``{"window": 5}``
    for an sma rule) — to the list-row projection. An unknown id returns
    ``{"status": "not_found"}``; a federation failure passes through as
    ``{"status": "domain_unavailable"}``.
    """
    rows = domain_query(
        _DB,
        "SELECT id, name, indicator_name, datasource, function_name,"
        " source_table, date_column, value_column, op, params_json, enabled\n"
        "FROM indicator_rules\n"
        "WHERE id = :iid",
        {"iid": int(indicator_id)},
    )
    if _fail(rows):
        return rows
    if not rows:
        return {"status": "not_found", "brand": _BRAND}
    return _project(rows[0], _INDICATOR_DETAIL_FIELDS)


def daas_list_rules(
    rule_type: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """List the DAAS unified rule catalog, optionally by rule type.

    Returns ``{results: [{id, name, rule_type, target, config_json,
    description, enabled, brand}], count, truncated}`` ordered by id.
    ``config_json`` is the rule's caliber (what it points at and how). An
    unknown rule_type matches nothing (truthful empty envelope); a domain
    failure passes through unchanged.
    """
    where, params = [], {"lim": _clamp(limit) + 1}
    if rule_type:
        params["rule_type"] = rule_type.strip()
        where.append("rule_type = :rule_type")
    sql = (
        "SELECT id, name, rule_type, target, config_json, description,"
        " enabled\n"
        "FROM rules"
    )
    if where:
        sql += "\nWHERE " + "\n  AND ".join(where)
    sql += "\nORDER BY id\nLIMIT :lim"
    rows = domain_query(_DB, sql, params)
    if _fail(rows):
        return rows
    return _envelope(rows, _RULE_FIELDS, _clamp(limit))


def daas_search_entities(
    keyword: str,
    entity_type: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Search DAAS entities (stocks + countries) by name / ticker / code.

    DATA READINESS: the entities mirror table is not yet provisioned in the
    daas store — the entity master is fd-open-data-mcp and the v1 sync
    whitelist covers only the 4 catalog tables — so until the table lands
    every call returns ``{"status": "domain_unavailable"}`` (fail-soft; a
    registered tool without its data is a legal state). Once provisioned,
    returns ``{results: [{id, entity_type, code, name, ticker, exchange,
    country_code, description, brand}], count, truncated}``;
    ``entity_type`` filters exactly (stock / country); a blank keyword
    yields a truthful empty envelope without querying.
    """
    q = (keyword or "").strip()
    if not q:
        return {"results": [], "count": 0, "truncated": False}
    limit = _clamp(limit)
    esc = _escape_like(q)
    params: dict[str, Any] = {
        "pat": f"%{esc}%", "q": q, "lim": limit + 1,
    }
    where = ["(name ILIKE :pat OR ticker ILIKE :pat OR code ILIKE :pat)"]
    if entity_type:
        params["entity_type"] = entity_type.strip()
        where.append("entity_type = :entity_type")
    sql = (
        "SELECT id, entity_type, code, name, ticker, exchange,"
        " country_code, description\n"
        "FROM entities\n"
        "WHERE " + "\n  AND ".join(where) + "\n"
        "ORDER BY (lower(name) = lower(:q)) DESC, id\n"
        "LIMIT :lim"
    )
    rows = domain_query(_DB, sql, params)
    if _fail(rows):
        return rows
    return _envelope(rows, _ENTITY_FIELDS, limit)


def daas_get_entity(entity_id: int) -> dict[str, Any]:
    """Read one DAAS entity's detail by id (from search).

    DATA READINESS: as with daas_search_entities — the entities mirror
    table is not yet provisioned in the daas store, so calls fail soft to
    ``{"status": "domain_unavailable"}`` until it lands. Once provisioned:
    the full entity row (``{id, entity_type, code, name, ticker, exchange,
    country_code, description, brand}``); an unknown id returns
    ``{"status": "not_found"}``.
    """
    rows = domain_query(
        _DB,
        "SELECT id, entity_type, code, name, ticker, exchange,"
        " country_code, description\n"
        "FROM entities\n"
        "WHERE id = :eid",
        {"eid": int(entity_id)},
    )
    if _fail(rows):
        return rows
    if not rows:
        return {"status": "not_found", "brand": _BRAND}
    return _project(rows[0], _ENTITY_FIELDS)
