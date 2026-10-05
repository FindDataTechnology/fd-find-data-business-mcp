"""DAAS supply-domain tests (facet-mcp-foundation-v1 tasks 4.3/4.4).

Follows the test_rows_returned.py recipe — the domain query layer is faked
for unit tests; wire assertions go through fastmcp's in-memory client; the
live-PG integration class mirrors test_tools_registry's needs_pg pattern.

Coverage map:
- metering: every envelope tool (the 5 search/list tools) stamps additive
  ``rows_returned`` == len(results) == count in-payload (never in result
  _meta); the 3 single-object get_* tools are not envelopes and stay
  unstamped; ``domain_unavailable`` pass-throughs are never stamped.
- fail-soft: a faked unavailable domain passes through unchanged for all 8
  tools; one real refused-socket test pins the domain layer's mapping; the
  entities tools are exercised against the live daas store, where the
  missing ``entities`` table must fail soft (registered tool, data pending
  is a legal state).
- whitelist / boundary: provenance columns (``config``, ``category_id``,
  ``score``, ``created_at``/``updated_at``/``synced_at``) never surface even
  when the faked row carries them; every served row is branded; blank
  keywords short-circuit without a query; limits clamp; LIKE wildcards in
  user input are neutralized.
- registration: exactly the 8 contract tools, all ``daas_``-prefixed.
- commercial closure: business-mcp's daas surface ⊆ the DAAS tool-surface
  manifest's explicit commercial set (manifest path via env
  DAAS_TOOL_SURFACE, default ../DAAS/fd-daas-mcp/... relative to this repo;
  skipped with the reason when the manifest file is absent).
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

os.environ.setdefault("FDBIZ_JWKS_URI", "")  # no auth in unit tests

import fd_find_data_business_mcp.server as biz  # noqa: E402
import fd_find_data_business_mcp.tools_daas as td  # noqa: E402

# Load the repo .env before the skipif markers evaluate (mirrors
# test_tools_registry.py); unit tests below never use it — domain_query is
# faked. The live integration class targets the real data-machine daas store.
_ENV = Path(__file__).resolve().parents[1] / ".env"
if not os.environ.get("FDBIZ_DOMAIN_PG_URL") and _ENV.exists():
    for _line in _ENV.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

needs_pg = pytest.mark.skipif(
    not os.environ.get("FDBIZ_DOMAIN_PG_URL"), reason="needs FDBIZ_DOMAIN_PG_URL")

DAAS_TOOLS = (
    "daas_list_indicators", "daas_get_indicator", "daas_search_entities",
    "daas_get_entity", "daas_list_rules", "daas_list_sources",
    "daas_search_functions", "daas_get_function_detail",
)

ENVELOPE_TOOLS = (
    "daas_list_indicators", "daas_search_entities", "daas_list_rules",
    "daas_list_sources", "daas_search_functions",
)

UNAVAILABLE = {"status": "domain_unavailable", "detail": "OperationalError"}


class FakeDomain:
    """Stand-in for tools_daas.domain_query: canned responses + recorded calls.

    ``responses`` is the list of query answers in call order — each answer is
    a list of row dicts (or the fail-soft dict itself). ``install([])``
    (no answers) answers every call with the empty result ``[]``.
    """

    def __init__(self, responses):
        self.responses = responses if isinstance(responses, list) else [responses]
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, dbname, sql, params=None):
        self.calls.append((dbname, sql, params or {}))
        if not self.responses:
            return []  # canned-nothing: the honest empty answer
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[idx]


def _call_tool(name: str, args: dict):
    """Invoke a registered tool over fastmcp's in-memory client (wire path)."""
    from fastmcp import Client

    async def go():
        async with Client(biz.mcp) as client:
            return await client.call_tool(name, args)
    return asyncio.run(go())


def _clean_meta(meta):
    return {k: v for k, v in (meta or {}).items()
            if not k.startswith("io.modelcontextprotocol")}


def _registered_tools(mcp):
    """FastMCP version-agnostic tool listing (3.x/4.x: async list_tools)."""
    try:
        tools = mcp.list_tools()
    except AttributeError:  # fastmcp < 2.7
        return mcp._tool_manager.list_tools()
    if hasattr(tools, "__await__"):
        tools = asyncio.run(tools)
    return tools


# --- faked catalog rows (shapes mirror the live daas mirror, 2026-10-06) ------

_DIRTY = {  # provenance/bookkeeping keys a drifting SELECT must never serve
    "created_at": "2026-06-23T20:36:20", "updated_at": "2026-07-04T06:49:20",
    "synced_at": "2026-10-06T08:00:00+00:00", "score": 20.0,
}

SOURCE_ROW = {"id": 2, "name": "cnstats", "label": "Chinese Statistics",
              "description": "National Bureau of Statistics macro indicators",
              "url": "https://data.stats.gov.cn/", "enabled": True,
              "config": {"api_key": "SECRET-CREDENTIALS"}, "category_id": 20,
              **_DIRTY}

FUNCTION_ROW = {"id": 1, "source_id": 25, "name": "reference_all_tickers",
                "label": "All Tickers",
                "description": "All ticker symbols supported",
                "category": "Reference",
                "parameters": {"path": "/v3/reference/tickers",
                               "method": "GET", "gated": False},
                "output_type": "DataFrame", "frequency": None, **_DIRTY}

INDICATOR_ROW = {"id": 1, "name": "SPY_ma5", "indicator_name": "SPY_ma5",
                 "datasource": "yfinance", "function_name": "SPY",
                 "source_table": "scraw_spy_daily", "date_column": "date",
                 "value_column": "Close", "op": "sma",
                 "params_json": {"window": 5}, "enabled": True, **_DIRTY}

RULE_ROW = {"id": 1, "name": "us_leadership_pool", "rule_type": "script",
            "target": "entity_ids",
            "config_json": {"script_path": "rules/entity_collections/us.py",
                            "function": "members"},
            "description": "migrated from rule_script", "enabled": True,
            **_DIRTY}

ENTITY_ROW = {"id": 1, "entity_type": "stock", "code": "AAPL",
              "name": "Apple Inc.", "ticker": "AAPL", "exchange": "NASDAQ",
              "country_code": "US", "description": "Cupertino hardware", **_DIRTY}


@pytest.fixture
def fake_daas(monkeypatch):
    """Patch tools_daas.domain_query; returns the FakeDomain for inspection."""
    holder: dict[str, FakeDomain] = {}

    def install(responses):
        fake = FakeDomain(responses)
        monkeypatch.setattr(td, "domain_query", fake)
        holder["fake"] = fake
        return fake

    holder["install"] = install
    return holder


# --- metering stamp (task 4.4: 计量盖章) ----------------------------------------

class TestMeteringStamp:
    def test_envelope_tools_stamp_rows_returned(self, fake_daas):
        rows = [SOURCE_ROW, SOURCE_ROW | {"id": 3, "name": "ckan"}]
        cases = [
            (biz.daas_list_sources, (None, None, 10), [rows]),
            (biz.daas_search_functions, ("ticker", None, None, 10), [[FUNCTION_ROW]]),
            (biz.daas_list_indicators, (None, None, None, 10), [[INDICATOR_ROW]]),
            (biz.daas_list_rules, (None, 10), [[RULE_ROW]]),
            (biz.daas_search_entities, ("apple", None, 10), [[ENTITY_ROW]]),
        ]
        for fn, args, canned in cases:
            fake_daas["install"](canned)
            out = fn(*args)
            assert out["rows_returned"] == len(out["results"]) == out["count"], fn.__name__
            assert {"results", "count", "truncated"} <= set(out), fn.__name__

    def test_wire_stamp_rides_payload_not_meta(self, fake_daas, monkeypatch):
        # scope gate opens a DB session; the wire contract under test here is
        # the stamp, not scoping — neutralize it like a None scope resolves.
        monkeypatch.setattr(biz, "_scope_gate", lambda scope, key: (None, None))
        fake_daas["install"]([[INDICATOR_ROW]])
        r = _call_tool("daas_list_indicators", {"keyword": "spy"})
        body = r.structured_content
        assert body["rows_returned"] == 1 and body["count"] == 1
        assert "rows_returned" not in _clean_meta(r.meta)

    def test_domain_unavailable_envelope_not_stamped(self, fake_daas):
        fake_daas["install"](UNAVAILABLE)
        out = biz.daas_list_rules(None, 10)
        assert out == UNAVAILABLE  # not a success: no rows_returned, no stamp
        assert "rows_returned" not in out

    def test_get_tools_are_not_envelopes_stay_unstamped(self, fake_daas):
        for fn, canned in (
            (biz.daas_get_indicator, [[INDICATOR_ROW]]),
            (biz.daas_get_entity, [[ENTITY_ROW]]),
            (biz.daas_get_function_detail, [[FUNCTION_ROW]]),
        ):
            fake_daas["install"](canned)
            out = fn(1)
            assert "results" not in out and "rows_returned" not in out
            assert out["brand"] == "finddata"


# --- fail-soft (task 4.4: PG 不可达 mock + 缺表降级) -----------------------------

class TestFailSoft:
    def test_all_eight_tools_pass_unavailable_through(self, fake_daas):
        fake_daas["install"](UNAVAILABLE)
        assert biz.daas_list_sources(None, None, 10) == UNAVAILABLE
        assert biz.daas_search_functions("x", None, None, 10) == UNAVAILABLE
        assert biz.daas_get_function_detail(1) == UNAVAILABLE
        assert biz.daas_list_indicators(None, None, None, 10) == UNAVAILABLE
        assert biz.daas_get_indicator(1) == UNAVAILABLE
        assert biz.daas_list_rules(None, 10) == UNAVAILABLE
        assert biz.daas_search_entities("x", None, 10) == UNAVAILABLE
        assert biz.daas_get_entity(1) == UNAVAILABLE

    def test_refused_socket_maps_to_unavailable(self, monkeypatch):
        # Real path through domain.query: port 1 on localhost refuses fast.
        from fd_find_data_business_mcp import domain

        monkeypatch.setenv("FDBIZ_DOMAIN_PG_URL",
                           "postgresql://u:p@127.0.0.1:1/daas")
        domain.dispose_all()
        try:
            out = td.daas_list_sources()
            assert out["status"] == "domain_unavailable"
            assert "detail" in out
        finally:
            domain.dispose_all()

    def test_entities_pending_is_fail_soft_not_a_raise(self, fake_daas):
        # The daas store has no entities table yet: PG answers UndefinedTable,
        # which domain.query maps to the same fail-soft dict. The tools must
        # pass it through — a registered tool without its data is a legal state.
        fake_daas["install"](UNAVAILABLE)
        assert biz.daas_search_entities("AAPL", None, 10) == UNAVAILABLE
        assert biz.daas_get_entity(1) == UNAVAILABLE


# --- whitelist / boundary (task 4.4: 白名单 + 越界拒绝) ---------------------------

class TestWhitelistAndBoundary:
    def test_provenance_columns_never_surface(self, fake_daas):
        fake = fake_daas["install"]([[SOURCE_ROW]])
        out = biz.daas_list_sources(None, None, 10)
        row = out["results"][0]
        assert set(row) == {"id", "name", "label", "description", "url",
                            "enabled", "brand"}
        assert row["brand"] == "finddata"
        for forbidden in td._FORBIDDEN_KEYS:
            assert forbidden not in row
        assert "config" in SOURCE_ROW and "synced_at" in SOURCE_ROW

    def test_detail_tools_serve_caliber_but_not_provenance(self, fake_daas):
        fake_daas["install"]([[FUNCTION_ROW]])
        out = biz.daas_get_function_detail(1)
        assert out["parameters"] == FUNCTION_ROW["parameters"]
        assert "synced_at" not in out and "config" not in out
        fake_daas["install"]([[INDICATOR_ROW]])
        out = biz.daas_get_indicator(1)
        assert out["params_json"] == {"window": 5}
        assert "synced_at" not in out
        fake_daas["install"]([[RULE_ROW]])
        out = biz.daas_list_rules(None, 10)
        assert out["results"][0]["config_json"] == RULE_ROW["config_json"]
        assert "synced_at" not in out["results"][0]

    def test_server_strip_cannot_leak_bare_source_key(self, fake_daas):
        # server._strip deletes keys named exactly "source"; the whitelists
        # must not rely on it (source_id survives, bare "source" never exists).
        fake_daas["install"]([[FUNCTION_ROW]])
        out = biz.daas_search_functions("ticker", None, None, 10)
        row = out["results"][0]
        assert row["source_id"] == 25 and "source" not in row

    def test_blank_keyword_short_circuits_without_query(self, fake_daas):
        fake = fake_daas["install"]([])
        for out in (biz.daas_search_functions("   ", None, None, 10),
                    biz.daas_search_entities("", None, 10)):
            assert out == {"results": [], "count": 0, "truncated": False,
                           "rows_returned": 0}
        assert fake.calls == []

    def test_limit_clamped_to_max(self, fake_daas):
        fake = fake_daas["install"]([])
        biz.daas_list_sources(None, None, 100000)
        assert fake.calls[0][2]["lim"] == td._MAX_LIMIT + 1
        biz.daas_search_functions("x", None, None, 0)  # 0/None floors at 1
        assert fake.calls[1][2]["lim"] == 2

    def test_like_wildcards_neutralized_in_params(self, fake_daas):
        fake = fake_daas["install"]([])
        biz.daas_search_functions("100%_a\\b", None, None, 10)
        pat = fake.calls[0][2]["pat"]
        assert pat == "%100\\%\\_a\\\\b%"

    def test_queries_target_the_daas_database(self, fake_daas):
        fake = fake_daas["install"]([])
        biz.daas_list_rules(None, 10)
        dbname, sql, _ = fake.calls[0]
        assert dbname == "daas"
        assert "FROM rules" in sql

    def test_unknown_ids_return_not_found(self, fake_daas):
        for fn, canned in (
            (biz.daas_get_indicator, [[]]),
            (biz.daas_get_entity, [[]]),
            (biz.daas_get_function_detail, [[]]),
        ):
            fake_daas["install"](canned)
            out = fn(424242)
            assert out == {"status": "not_found", "brand": "finddata"}


# --- scope discipline (wrappers wire the daas source into the gate) --------------

REGISTRY_DDL = """
CREATE TABLE registry_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_db VARCHAR(64), source_table VARCHAR(128), source_column VARCHAR(128),
  native_code VARCHAR(128), semantic_code VARCHAR(128),
  name_zh VARCHAR(255), name_en VARCHAR(255), unit VARCHAR(64),
  frequency VARCHAR(32), domain VARCHAR(64), verified INTEGER DEFAULT 0
)
"""


@pytest.fixture
def scoped_db(tmp_path, monkeypatch):
    """fd_open_data_mcp DB with the scoping tables + registry anchor rows.

    scope_create validates its source_dbs against registry_entries, so the
    fixture seeds one anchor row per source_db the tests scope with.
    """
    from sqlalchemy import text

    from fd_open_data_mcp import db as dbmod
    from fd_open_data_mcp.models import Base

    monkeypatch.setenv("FD_OPEN_DATA_MCP_DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    dbmod.reset_database()
    db = dbmod.get_database()
    Base.metadata.create_all(db.engine)
    s = db.get_session()
    s.execute(text(REGISTRY_DDL))
    for source_db in ("daas", "law_db"):
        s.execute(text(
            "INSERT INTO registry_entries (source_db, source_table, source_column,"
            " native_code, semantic_code, name_zh, name_en, unit, frequency,"
            " domain, verified) VALUES "
            f"('{source_db}','t','v','X','{source_db}.x','n','n','u','yearly','d',1)"))
    s.commit()
    s.close()
    yield db
    dbmod.reset_database()


class TestScopeDiscipline:
    def test_unknown_scope_errors_explicitly(self, scoped_db, fake_daas):
        fake_daas["install"]([])
        out = biz.daas_list_indicators(None, None, None, 10, scope="ghost")
        assert out["error"] == "unknown_scope" and out["scope"] == "ghost"

    def test_scope_excluding_daas_is_explainable_empty(self, scoped_db, fake_daas):
        from fd_open_data_mcp import scoping

        s = scoped_db.get_session()
        scoping.scope_create(s, "law_only", {"source_dbs": ["law_db"]})
        s.close()
        fake = fake_daas["install"]([])
        out = biz.daas_list_rules(None, 10, scope="law_only")
        assert out["count"] == 0 and out["status"] == "out_of_scope"
        assert fake.calls == []  # gate rejects before any domain query

    def test_admitted_scope_disclosed_and_counted(self, scoped_db, fake_daas):
        from fd_open_data_mcp import scoping
        from fd_open_data_mcp.models import ScopeStat

        s = scoped_db.get_session()
        scoping.scope_create(s, "daas_only", {"source_dbs": ["daas"]})
        s.close()
        fake_daas["install"]([[RULE_ROW]])
        out = biz.daas_list_rules(None, 10, scope="daas_only")
        assert out["count"] == 1 and out["rows_returned"] == 1
        assert out["scope"]["name"] == "daas_only"
        s = scoped_db.get_session()
        stats = s.query(ScopeStat).filter_by(scope_name="daas_only").all()
        s.close()
        assert len(stats) == 1 and stats[0].calls == 1

    def test_unscoped_call_has_no_scope_key(self, scoped_db, fake_daas):
        fake_daas["install"]([])
        out = biz.daas_list_rules(None, 10)
        assert "scope" not in out


# --- registration surface ---------------------------------------------------------

class TestRegistration:
    def test_all_eight_daas_tools_registered(self):
        names = {t.name for t in _registered_tools(biz.mcp)}
        missing = [n for n in DAAS_TOOLS if n not in names]
        assert not missing, missing

    def test_daas_tools_are_prefixed(self):
        names = {t.name for t in _registered_tools(biz.mcp)}
        daas_surface = {n for n in names if n.startswith("daas_")}
        assert daas_surface == set(DAAS_TOOLS)  # no drift, no strays

    def test_tool_sources_declares_the_daas_source(self):
        assert biz._TOOL_SOURCES["daas"] == ["daas"]


# --- commercial closure (task 4.4: 商业集闭合) -------------------------------------

def _manifest_path() -> Path:
    env = os.environ.get("DAAS_TOOL_SURFACE")
    if env:
        return Path(env)
    # default: the sibling DAAS checkout, relative to this repo root
    return (Path(__file__).resolve().parents[2]
            / "DAAS" / "fd-daas-mcp" / "daas" / "fd_daas_mcp"
            / "tool_surface.yaml")


def _commercial_set(path: Path) -> set[str]:
    """The manifest's explicit commercial set, namespaced ``<group>_<tool>``.

    Mirrors daas/fd_daas_mcp/selfcheck.py:commercial_tool_set — only EXPLICIT
    per-tool ``commercial`` entries count (group-level posture never inherits).
    Parsed directly from the YAML so the check stays importable from this repo
    without loading the DAAS server's registry.
    """
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {f"{group}_{tool}"
            for group, gspec in (data.get("groups") or {}).items()
            for tool, level in ((gspec or {}).get("tools") or {}).items()
            if level == "commercial"}


class TestCommercialClosure:
    def test_business_daas_surface_subset_of_manifest_commercial(self):
        p = _manifest_path()
        if not p.exists():
            pytest.skip(f"DAAS tool-surface manifest not found at {p} "
                        f"(set DAAS_TOOL_SURFACE to override)")
        commercial = _commercial_set(p)
        names = {t.name for t in _registered_tools(biz.mcp)}
        surface = {n for n in names if n.startswith("daas_")}
        # the contract: business-mcp daas tools ⊆ DAAS manifest commercial set
        leak = surface - commercial
        assert not leak, f"daas tools outside the manifest commercial set: {sorted(leak)}"
        # and the 8 business anchors are all present on both sides
        assert set(DAAS_TOOLS) <= commercial
        assert surface == set(DAAS_TOOLS)


# --- live integration (real data-machine daas store) ------------------------------

@needs_pg
class TestLiveDaasStore:
    def test_catalog_counts_match_the_2026_10_06_bootstrap(self):
        # full-replace sync keeps source == target; assert the bootstrap
        # counts as floors (natural catalog growth tolerated).
        out = td.daas_list_sources(limit=td._MAX_LIMIT)
        assert out["count"] >= 21
        out = td.daas_list_indicators(limit=td._MAX_LIMIT)
        assert out["count"] >= 200  # truncated at the cap; total is 391
        assert out["truncated"] is True
        out = td.daas_list_rules()
        assert out["count"] >= 1

    def test_rows_branded_and_provenance_free(self):
        out = td.daas_list_sources(limit=5)
        for row in out["results"]:
            assert row["brand"] == "finddata"
            assert not ({"config", "score", "synced_at", "created_at",
                         "updated_at", "category_id"} & set(row))

    def test_function_search_and_detail_roundtrip(self):
        out = td.daas_search_functions("ticker", limit=5)
        assert out["count"] >= 1
        fid = out["results"][0]["id"]
        assert "parameters" not in out["results"][0]  # caliber rides in detail
        detail = td.daas_get_function_detail(fid)
        assert detail["id"] == fid
        assert isinstance(detail["parameters"], dict)
        assert "parameters" in detail and detail["brand"] == "finddata"

    def test_indicator_get_returns_full_caliber(self):
        out = td.daas_list_indicators(limit=5)
        assert out["count"] >= 1
        iid = out["results"][0]["id"]
        assert "params_json" not in out["results"][0]
        one = td.daas_get_indicator(iid)
        assert one["id"] == iid and "params_json" in one
        assert one["op"] and one["source_table"]

    def test_entities_table_pending_fails_soft_end_to_end(self):
        # The daas store has no entities table (outside the v1 sync scope).
        # The registered tools must answer the fail-soft envelope, never raise.
        out = td.daas_search_entities("AAPL")
        assert out.get("status") == "domain_unavailable", out
        out = td.daas_get_entity(1)
        assert out.get("status") == "domain_unavailable", out
