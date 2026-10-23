"""Unified indicator-series read (series_router + indicator_series_read).

Unit tests fake the routers at the read_series boundary and the semantic
router's session/reads at module level — no network, no DB. Wire assertions
go through fastmcp's in-memory client for the tool envelope itself.
"""
from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("FDBIZ_JWKS_URI", "")  # no auth in unit tests

import fd_find_data_business_mcp.series_router as sr  # noqa: E402
import fd_find_data_business_mcp.server as biz  # noqa: E402

_FORBIDDEN = ("source", "source_used", "real_source_used", "real_source", "table_id")


def _call_tool(name: str, args: dict):
    from fastmcp import Client

    async def _run():
        async with Client(biz.mcp) as client:
            return await client.call_tool(name, args)

    return asyncio.run(_run())

_ROW = {"indicator_id": "yearbook_catalog:39709", "date": "2024-12-31",
        "value": 54188.0, "unit": "元", "brand": "finddata"}


# --- split_indicator_id --------------------------------------------------------

class TestSplit:
    def test_valid_split(self):
        assert sr.split_indicator_id("yearbook_catalog:39709") == (
            "yearbook_catalog", "39709")

    def test_rejects_missing_colon_and_extra_colon(self):
        for bad in ("yearbook_catalog", "a:b:c", "a::b", ":x", "x:", " "):
            with pytest.raises(sr.SeriesRouteError):
                sr.split_indicator_id(bad)


# --- read_series routing ---------------------------------------------------------

class TestRouting:
    def test_unsupported_source_db_rejected(self):
        with pytest.raises(sr.SeriesRouteError) as ei:
            sr.read_series("wenshu_db:42", "region", "全国", "2020", "2024")
        assert "暂不支持序列拉取" in str(ei.value)
        assert "wenshu_db" in str(ei.value)

    def test_yearbook_wrong_entity_type_rejected(self):
        with pytest.raises(sr.SeriesRouteError) as ei:
            sr.read_series("yearbook_catalog:39709", "country", "CHN", "2020", "2024")
        assert "region" in str(ei.value)

    def test_yearbook_non_numeric_code_rejected(self):
        with pytest.raises(sr.SeriesRouteError) as ei:
            sr.read_series("yearbook_catalog:人均GDP", "region", "全国", "2020", "2024")
        assert "解析失败" in str(ei.value)

    def test_wb_wrong_entity_type_rejected(self):
        with pytest.raises(sr.SeriesRouteError) as ei:
            sr.read_series("world_bank:NY.GDP.PCAP.CD", "region", "全国", "2020", "2024")
        assert "country" in str(ei.value)


# --- routers against faked impls -------------------------------------------------

class TestRouters:
    def test_yearbook_rows_normalized(self, monkeypatch):
        from fd_find_data_business_mcp import tools_yearbook as ty

        def fake_read(iid, region=None, start_year=None, end_year=None, limit=200,
                      offset=0, province=None):
            assert iid == 39709 and region == "北京市"
            assert start_year == 2020 and end_year == 2024
            return {"results": [
                {"year": 2020, "region": "北京市", "value": 40000, "unit": "元",
                 "brand": "finddata"},
                {"year": 2024, "region": "北京市", "value": 54188, "unit": "元",
                 "brand": "finddata"},
            ], "count": 2, "truncated": False, "next_offset": None}

        monkeypatch.setattr(ty, "yearbook_read", fake_read)
        rows = sr._yearbook_router("39709", "region", "北京市", "2020", "2024")
        assert rows[0]["date"] == "2020-12-31"
        assert rows[1]["indicator_id"] == "yearbook_catalog:39709"
        assert rows[1]["brand"] == "finddata"
        assert not any(k in rows[1] for k in _FORBIDDEN)

    def test_yearbook_domain_unavailable_raises(self, monkeypatch):
        from fd_find_data_business_mcp import tools_yearbook as ty

        monkeypatch.setattr(ty, "yearbook_read",
                            lambda *a, **k: {"status": "domain_unavailable",
                                             "detail": "OperationalError"})
        with pytest.raises(sr.SeriesRouteError) as ei:
            sr._yearbook_router("39709", "region", "全国", None, None)
        assert "暂不可用" in str(ei.value)

    def test_wb_rows_normalized(self, monkeypatch):
        from fd_find_data_business_mcp import tools_wb as twb

        def fake_read(code, countries, sy=None, ey=None, limit=600):
            assert code == "NY.GDP.PCAP.CD" and countries == ["CHN"]
            assert sy == 2019 and ey == 2024
            return {"results": [
                {"year": 2019, "country": "CHN", "country_name": "中国",
                 "value": 10217, "unit": "USD", "brand": "finddata"},
            ], "count": 1, "truncated": False}

        monkeypatch.setattr(twb, "wb_read", fake_read)
        rows = sr._world_bank_router("NY.GDP.PCAP.CD", "country", "CHN",
                                     "2019", "2024")
        assert rows == [{"indicator_id": "world_bank:NY.GDP.PCAP.CD",
                         "date": "2019-12-31", "value": 10217, "unit": "USD",
                         "brand": "finddata"}]

    def test_wb_domain_unavailable_raises(self, monkeypatch):
        from fd_find_data_business_mcp import tools_wb as twb

        monkeypatch.setattr(twb, "wb_read",
                            lambda *a, **k: {"status": "domain_unavailable",
                                             "detail": "boom"})
        with pytest.raises(sr.SeriesRouteError):
            sr._world_bank_router("NY.GDP.PCAP.CD", "country", "CHN", None, None)


# --- semantic router: session-level fakes ----------------------------------------

class TestSemanticRouter:
    def test_unresolvable_code_rejected(self, monkeypatch):
        class FakeQ:
            def filter(self, *a):
                return self

            def first(self):
                return None

        class FakeS:
            def query(self, *a):
                return FakeQ()

            def close(self):
                pass

        class FakeDB:
            def get_session(self):
                return FakeS()

        import fd_open_data_mcp.db as odb
        monkeypatch.setattr(odb, "get_database", lambda: FakeDB())
        with pytest.raises(sr.SeriesRouteError) as ei:
            sr._semantic_router("no_such_code", "country", 1, "2020", "2024")
        assert "解析失败" in str(ei.value)

    def test_duplicate_dates_deduped_first_wins(self, monkeypatch):
        import fd_open_data_mcp.db as odb
        import fd_open_data_mcp.fetch.cache as cache_mod
        import fd_open_data_mcp.models as models

        concept = models.Concept(id=7, code="gdp", entity_type="country",
                                 frequency="daily")

        class FakeQ:
            def filter(self, *a):
                return self

            def first(self):
                return concept

        class FakeS:
            def query(self, *a):
                return FakeQ()

            def close(self):
                pass

        class FakeDB:
            def get_session(self):
                return FakeS()

        class Obs:
            def __init__(self, d, v):
                self.date, self.value, self.unit = d, v, ""

        monkeypatch.setattr(odb, "get_database", lambda: FakeDB())
        monkeypatch.setattr(cache_mod, "read_cache_range",
                            lambda s, cid, et, eid, st, en:
                            [Obs("2024-01-01", 49.5), Obs("2024-01-01", 49.6),
                             Obs("2024-02-01", 49.2)])
        rows = sr._semantic_router("gdp", "country", 1, "2024-01-01", "2024-12-31")
        assert [r["date"] for r in rows] == ["2024-01-01", "2024-02-01"]
        assert rows[0]["value"] == 49.5  # first (highest-ranked) occurrence

    def test_cached_rows_normalized(self, monkeypatch):
        import fd_open_data_mcp.db as odb
        import fd_open_data_mcp.fetch.cache as cache_mod
        import fd_open_data_mcp.models as models

        concept = models.Concept(id=7, code="gdp", entity_type="country",
                                 frequency="daily")

        class FakeQ:
            def filter(self, *a):
                return self

            def first(self):
                return concept

        class FakeS:
            def query(self, *a):
                return FakeQ()

            def close(self):
                pass

        class FakeDB:
            def get_session(self):
                return FakeS()

        class Obs:
            date, value, unit = "2024-01-01", "1041.2", "亿美元"

        monkeypatch.setattr(odb, "get_database", lambda: FakeDB())
        monkeypatch.setattr(cache_mod, "read_cache_range",
                            lambda s, cid, et, eid, st, en: [Obs()])
        rows = sr._semantic_router("gdp", "country", 1, "2024-01-01", "2024-12-31")
        assert rows == [{"indicator_id": "fd_open_data:gdp", "date": "2024-01-01",
                         "value": 1041.2, "unit": "亿美元", "brand": "finddata"}]


# --- tool envelope via in-memory client ------------------------------------------

class TestToolEnvelope:
    def test_batch_isolation_and_rows_returned(self, monkeypatch):
        import fd_find_data_business_mcp.series_router as srx

        def fake_read_series(iid, entity_type, entity_id, start, end):
            if iid.startswith("world_bank"):
                raise srx.SeriesRouteError(f"source_db world_bank 暂不支持序列拉取")
            return [dict(_ROW, indicator_id=iid)]

        monkeypatch.setattr(srx, "read_series", fake_read_series)
        out = _call_tool(
            "indicator_series_read",
            {"indicator_ids": ["yearbook_catalog:39709", "world_bank:X"],
             "entity_type": "region", "entity_id": "全国",
             "start": "2020", "end": "2024"},
        )
        structured = out.structured_content
        assert structured["rows_returned"] == 1
        assert structured["results"][0]["indicator_id"] == "yearbook_catalog:39709"
        assert structured["errors"] == [
            {"indicator_id": "world_bank:X",
             "error": "source_db world_bank 暂不支持序列拉取"}]

    def test_brand_no_leak(self, monkeypatch):
        import fd_find_data_business_mcp.series_router as srx

        leaky = dict(_ROW, source="upstream", real_source="stat.gov.cn")
        monkeypatch.setattr(srx, "read_series", lambda *a: [leaky])
        out = _call_tool(
            "indicator_series_read",
            {"indicator_ids": ["yearbook_catalog:1"], "entity_type": "region",
             "entity_id": "全国"},
        )
        structured = out.structured_content
        row = structured["results"][0]
        assert not any(k in row for k in _FORBIDDEN)
