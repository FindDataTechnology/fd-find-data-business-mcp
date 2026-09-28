"""Tests for the registry coverage tool (tools_registry.py).

Unit tests fake the domain query — no network, no live registry. The
fixture rows mirror the first registration batch (2026-09-22 census):
world_bank 1,499 (all verified), yearbook_catalog 53,925 (all unverified),
gta_panel 1,468 (1,466 verified), china_city_panel 209 (all verified),
fd_open_data 452 (246 verified); law_db deliberately has zero entries
(document-domain caliber).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import fd_find_data_business_mcp.tools_registry as tr

# Load the repo .env before the skipif markers evaluate (mirrors the other
# tool test modules); unit tests below never use it — domain_query is faked.
_ENV = Path(__file__).resolve().parents[1] / ".env"
if not os.environ.get("FDBIZ_DOMAIN_PG_URL") and _ENV.exists():
    for _line in _ENV.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

needs_pg = pytest.mark.skipif(
    not os.environ.get("FDBIZ_DOMAIN_PG_URL"), reason="needs FDBIZ_DOMAIN_PG_URL")


class FakeDomain:
    """Stand-in for domain.query: canned rows, recorded calls."""

    def __init__(self, response):
        self.response = response
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, dbname, sql, params=None):
        self.calls.append((dbname, sql, params or {}))
        return self.response


def _live(source_db, registered, verified):
    """Shape of the GROUP BY projection for one source_db."""
    return {"source_db": source_db, "registered": registered,
            "verified": verified, "unverified": registered - verified}


def _first_batch():
    """The five registered sources exactly as the census describes them."""
    return [
        _live("world_bank", 1499, 1499),
        _live("yearbook_catalog", 53925, 0),
        _live("gta_panel", 1468, 1466),
        _live("china_city_panel", 209, 209),
        _live("fd_open_data", 452, 246),
    ]


def _by_source(rows):
    return {r["source_db"]: r for r in rows}


class TestCoverageUnit:
    def test_envelope_lists_all_sources_with_live_counts(self, monkeypatch):
        fake = FakeDomain(_first_batch())
        monkeypatch.setattr(tr, "domain_query", fake)
        r = tr.registry_coverage()
        assert r["count"] == 8  # 5 registered + ia35 + wenshu_db + law_db
        assert r["census_as_of"] == "2026-09-22"
        assert "note" not in r  # live counts served — no degradation note
        by = _by_source(r["results"])
        assert by["world_bank"] == {
            "source_db": "world_bank", "label_zh": "世界银行 WDI 宏观指标",
            "phase": "第一期（已登记）", "registered": 1499, "verified": 1499,
            "unverified": 0, "actual_in_source": 1499, "coverage": 1.0}
        assert by["yearbook_catalog"]["registered"] == 53925
        assert by["yearbook_catalog"]["unverified"] == 53925
        assert by["yearbook_catalog"]["coverage"] == 1.0
        assert by["gta_panel"]["verified"] == 1466
        assert by["gta_panel"]["unverified"] == 2
        assert by["china_city_panel"]["coverage"] == 1.0
        assert by["fd_open_data"]["verified"] == 246
        assert by["fd_open_data"]["unverified"] == 206

    def test_phase2_sources_declared_unregistered(self, monkeypatch):
        monkeypatch.setattr(tr, "domain_query", FakeDomain(_first_batch()))
        r = tr.registry_coverage()
        by = _by_source(r["results"])
        ia35 = by["ia35"]
        assert ia35["phase"] == "第二期（未登记）"
        assert ia35["registered"] == 0 and ia35["verified"] == 0
        assert ia35["coverage"] is None  # no census anchor -> not computable
        assert "工商注册" in ia35["label_zh"]
        wenshu = by["wenshu_db"]
        assert wenshu["registered"] == 0 and wenshu["phase"] == "第二期（未登记）"
        assert "裁判文书" in wenshu["label_zh"]

    def test_law_db_document_domain_caliber_is_expected_zero(self, monkeypatch):
        monkeypatch.setattr(tr, "domain_query", FakeDomain(_first_batch()))
        r = tr.registry_coverage()
        law = _by_source(r["results"])["law_db"]
        assert law["registered"] == 0 and law["verified"] == 0
        assert law["unverified"] == 0
        assert law["phase"] == "文档域（非指标口径）"
        assert "非指标口径" in law["note"]
        assert "登记 0 属预期" in law["note"]

    def test_verified_split_sums_to_registered(self, monkeypatch):
        monkeypatch.setattr(tr, "domain_query", FakeDomain(_first_batch()))
        r = tr.registry_coverage()
        for row in r["results"]:
            assert row["verified"] + row["unverified"] == row["registered"], row

    def test_coverage_tracks_census_anchor(self, monkeypatch):
        # Natural growth: 210 registered city variables against the 209
        # census anchor -> coverage slightly above 1.
        rows = _first_batch()
        rows[3] = _live("china_city_panel", 210, 209)
        monkeypatch.setattr(tr, "domain_query", FakeDomain(rows))
        r = tr.registry_coverage()
        city = _by_source(r["results"])["china_city_panel"]
        assert city["coverage"] == round(210 / 209, 4)
        assert city["actual_in_source"] == 209  # census anchor stays static

    def test_source_registered_but_undeclared_is_surfaced(self, monkeypatch):
        rows = _first_batch() + [_live("new_source", 5, 5)]
        monkeypatch.setattr(tr, "domain_query", FakeDomain(rows))
        r = tr.registry_coverage()
        extra = _by_source(r["results"])["new_source"]
        assert extra["registered"] == 5
        assert extra["phase"] == "已登记（未声明）"
        assert r["count"] == 9

    def test_query_targets_registry_db_with_group_by(self, monkeypatch):
        fake = FakeDomain([])
        monkeypatch.setattr(tr, "domain_query", fake)
        tr.registry_coverage()
        assert len(fake.calls) == 1  # single live query
        dbname, sql, params = fake.calls[0]
        assert dbname == "fd_open_data"
        assert "registry_entries" in sql
        assert "GROUP BY source_db" in sql
        assert "FILTER (WHERE verified)" in sql
        assert params == {}

    def test_unavailable_registry_serves_static_part_with_note(self, monkeypatch):
        fail = {"status": "domain_unavailable", "detail": "OperationalError"}
        monkeypatch.setattr(tr, "domain_query", FakeDomain(fail))
        r = tr.registry_coverage()
        assert "note" in r and "unavailable" in r["note"]
        assert r["count"] == 8
        by = _by_source(r["results"])
        # Phase-1 rows fall back to the census anchor; the verified split
        # is unknown without the registry.
        assert by["world_bank"]["registered"] == 1499
        assert by["world_bank"]["verified"] is None
        assert by["world_bank"]["unverified"] is None
        assert by["world_bank"]["coverage"] == 1.0
        # Phase-2 / document-domain rows are fully static and unchanged.
        assert by["ia35"]["registered"] == 0
        assert by["law_db"]["registered"] == 0

    def test_rows_expose_no_exact_source_key(self, monkeypatch):
        # server._strip deletes keys named exactly "source"; source_db must
        # survive it, and no row may carry a bare "source" key.
        monkeypatch.setattr(tr, "domain_query", FakeDomain(_first_batch()))
        r = tr.registry_coverage()
        for row in r["results"]:
            assert "source" not in row
            assert "source_db" in row


class TestServerRegistration:
    def test_registry_coverage_is_a_registered_tool(self):
        from fd_find_data_business_mcp import server

        names = {t.name for t in _registered_tools(server.mcp)}
        assert "registry_coverage" in names

    def test_registered_search_tools_still_present(self):
        from fd_find_data_business_mcp import server

        names = {t.name for t in _registered_tools(server.mcp)}
        assert {"yearbook_search_indicators", "wb_search_indicators",
                "gta_search_variables", "city_search_variables"} <= names

    def test_strip_keeps_source_db_and_registry_fields(self, monkeypatch):
        # Brand isolation deletes keys named exactly "source"; the coverage
        # rows (source_db / semantic_code / native_code) must survive it.
        from fd_find_data_business_mcp import server

        monkeypatch.setattr(tr, "domain_query", FakeDomain(_first_batch()))
        payload = tr.registry_coverage()
        stripped = server._strip(payload)
        assert stripped == payload  # nothing in the envelope is stripped
        assert all("source_db" in row for row in stripped["results"])


def _registered_tools(mcp):
    """FastMCP version-agnostic tool listing (3.x: async list_tools)."""
    import asyncio
    try:
        tools = mcp.list_tools()
    except AttributeError:  # fastmcp < 2.7
        return mcp._tool_manager.list_tools()
    if hasattr(tools, "__await__"):
        tools = asyncio.run(tools)
    return tools


@needs_pg
class TestCoverageIntegration:
    def test_live_registry_counts_match_first_batch_caliber(self):
        r = tr.registry_coverage()
        assert "note" not in r, r.get("note")
        by = _by_source(r["results"])
        # First-batch anchors (census 2026-09-22); natural growth tolerated.
        assert by["world_bank"]["registered"] >= 1499
        assert by["yearbook_catalog"]["registered"] >= 53925
        assert by["gta_panel"]["registered"] >= 1468
        assert by["china_city_panel"]["registered"] >= 209
        assert by["fd_open_data"]["registered"] >= 452
        # law_db is document-domain by decision: zero entries is expected.
        assert by["law_db"]["registered"] == 0
        for row in r["results"]:
            assert row["verified"] + row["unverified"] == row["registered"]
