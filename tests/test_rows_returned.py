"""rows_returned metering contract (change: wire-platform-v1, task 1.1).

Two stamping channels, chosen by each tool's existing payload style:

- Envelope-shaped domain reads (yearbook_read, wb_read, gta_read, city_read,
  ia35_reg_counts_read): additive in-envelope ``rows_returned`` ==
  len(results) — these tools were envelope-style from the start, so a new
  envelope field is the additive change.
- Bare-payload core reads (``read``: bare list; ``read_range``: bare
  ``{concept_id: rows}`` map): the wire payload stays byte-identical to the
  pre-change implementation (golden fixtures below were captured from the
  pristine tree over the in-memory client), and ``rows_returned`` rides the
  protocol-standard MCP result ``_meta`` via a fastmcp middleware.

All unit-level: the domain query layer and the crawled-cache reads are
faked; wire assertions go through fastmcp's in-memory client — the same
serialization path real clients see.
"""
from __future__ import annotations

import asyncio
import json
import os

import pytest

os.environ.setdefault("FDBIZ_JWKS_URI", "")  # no auth in unit tests

import fd_find_data_business_mcp.server as biz  # noqa: E402
import fd_find_data_business_mcp.tools_city as tc  # noqa: E402
import fd_find_data_business_mcp.tools_firms as tf  # noqa: E402
import fd_find_data_business_mcp.tools_gta as tg  # noqa: E402
import fd_open_data_mcp.fetch.cache as cache_mod  # noqa: E402
import fd_open_data_mcp.fetch.dispatch as dispatch_mod  # noqa: E402
import fd_find_data_business_mcp.tools_wb as tw  # noqa: E402
import fd_find_data_business_mcp.tools_yearbook as ty  # noqa: E402

#: The read tools the metering contract covers (exactly the observation-
#: serving surface; search/document tools are out of caliber).
STAMPED_READ_TOOLS = (
    "read", "read_range", "yearbook_read", "wb_read", "gta_read",
    "city_read", "ia35_reg_counts_read",
)


class FakeDomain:
    """Stand-in for domain.query: canned rows per call, in call order."""

    def __init__(self, responses):
        self.responses = responses if isinstance(responses, list) else [responses]
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, dbname, sql, params=None):
        self.calls.append((dbname, params or {}))
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[idx]


class _FakeSession:
    def close(self):
        pass


class _Obs:
    """Cached-observation stand-in (read path: .value / .unit)."""

    def __init__(self, value, unit):
        self.value, self.unit = value, unit


class _RangeRow:
    """Cached range-row stand-in (attribute access, like read_cache_range)."""

    def __init__(self, date, value, unit):
        self.date, self.value, self.unit = date, value, unit


# --- golden wire fixtures (captured from the pristine implementation) ----------
# Byte-level shape of read/read_range BEFORE the change, recorded over the
# in-memory client with exactly the fakes below. The current implementation
# must reproduce content text and structuredContent verbatim; only result
# _meta gains rows_returned.

READ_DATES = ["2024-01-01", "2024-01-02", "2024-01-03"]
READ_HITS = {"2024-01-01": _Obs("123.4", "亿元"),
             "2024-01-03": _Obs("125.6", "亿元")}
READ_GOLDEN_TEXT = (
    '[{"date":"2024-01-01","value":123.4,"unit":"亿元","brand":"finddata"},'
    '{"date":"2024-01-02","value":null,"status":"unavailable"},'
    '{"date":"2024-01-03","value":125.6,"unit":"亿元","brand":"finddata"}]'
)

RANGE_ROWS = {
    7: [_RangeRow(f"2020-01-0{i}", str(i), "万人") for i in (1, 2, 3)],
    8: [_RangeRow(f"2021-01-0{i}", str(i), "万人") for i in (1, 2, 3, 4)],
}
READ_RANGE_GOLDEN_TEXT = (
    '{"7":['
    '{"date":"2020-01-01","value":1.0,"unit":"万人","brand":"finddata"},'
    '{"date":"2020-01-02","value":2.0,"unit":"万人","brand":"finddata"},'
    '{"date":"2020-01-03","value":3.0,"unit":"万人","brand":"finddata"}],'
    '"8":['
    '{"date":"2021-01-01","value":1.0,"unit":"万人","brand":"finddata"},'
    '{"date":"2021-01-02","value":2.0,"unit":"万人","brand":"finddata"},'
    '{"date":"2021-01-03","value":3.0,"unit":"万人","brand":"finddata"},'
    '{"date":"2021-01-04","value":4.0,"unit":"万人","brand":"finddata"}],'
    '"9":[]}'
)


@pytest.fixture
def fake_session(monkeypatch):
    monkeypatch.setattr(biz, "_session", lambda: _FakeSession())


@pytest.fixture
def golden_reads(fake_session, monkeypatch):
    """The exact fake answers the golden fixtures were captured with."""
    monkeypatch.setattr(cache_mod, "read_cache",
                        lambda s, c, t, e, d: READ_HITS.get(d))
    monkeypatch.setattr(cache_mod, "read_cache_range",
                        lambda s, c, t, e, start, end: RANGE_ROWS.get(c, []))
    monkeypatch.setattr(dispatch_mod, "read_range",
                        lambda s, c, t, e, start, end: {})


def _call_tool(name: str, args: dict):
    """Invoke a registered tool over fastmcp's in-memory client (wire path)."""
    from fastmcp import Client

    async def go():
        async with Client(biz.mcp) as client:
            return await client.call_tool(name, args)
    return asyncio.run(go())


def _clean_meta(meta):
    """Drop keys the in-memory client adds for its own bookkeeping."""
    return {k: v for k, v in (meta or {}).items()
            if not k.startswith("io.modelcontextprotocol")}


# --- read: bare payload pinned + rows_returned in result._meta -----------------

class TestReadWireContract:
    def test_payload_byte_identical_and_meta_stamped(self, golden_reads):
        r = _call_tool("read", {"concept_id": 1, "entity_type": "city",
                                "entity_id": 1101, "dates": READ_DATES})
        assert [b.text for b in r.content] == [READ_GOLDEN_TEXT]
        assert r.structured_content == {"result": json.loads(READ_GOLDEN_TEXT)}
        meta = _clean_meta(r.meta)
        assert meta["rows_returned"] == 2  # unavailable placeholder not counted
        # fastmcp's own wrap marker is preserved, not replaced
        assert meta.get("fastmcp") == {"wrap_result": True}

    def test_all_unavailable_meta_zero_payload_bare(self, fake_session,
                                                    monkeypatch):
        monkeypatch.setattr(cache_mod, "read_cache",
                            lambda s, c, t, e, d: None)
        r = _call_tool("read", {"concept_id": 1, "entity_type": "city",
                                "entity_id": 1101, "dates": READ_DATES})
        assert r.structured_content == {"result": [
            {"date": d, "value": None, "status": "unavailable"}
            for d in READ_DATES]}
        assert _clean_meta(r.meta)["rows_returned"] == 0

    def test_in_process_call_returns_bare_list(self, golden_reads):
        """Direct callers keep the original bare-list return (调用行为不变)."""
        out = biz.read(1, "city", 1101, READ_DATES)
        assert out == json.loads(READ_GOLDEN_TEXT)
        assert isinstance(out, list)


# --- read_range: bare map pinned + rows_returned in result._meta ---------------

class TestReadRangeWireContract:
    def test_payload_byte_identical_and_meta_stamped(self, golden_reads):
        r = _call_tool("read_range", {"concept_ids": [7, 8, 9],
                                      "entity_type": "country",
                                      "entity_id": 156,
                                      "start": "2020-01-01",
                                      "end": "2021-12-31"})
        assert [b.text for b in r.content] == [READ_RANGE_GOLDEN_TEXT]
        assert r.structured_content == json.loads(READ_RANGE_GOLDEN_TEXT)
        meta = _clean_meta(r.meta)
        # 3 + 4 rows served, concept 9 cold and empty; pristine tree sent no _meta
        assert meta == {"rows_returned": 7}

    def test_rows_returned_equals_total_rows_120(self, fake_session,
                                                 monkeypatch):
        rows = [_RangeRow(f"{1900 + i:04d}-12-31", str(i), "万人")
                for i in range(120)]
        monkeypatch.setattr(cache_mod, "read_cache_range",
                            lambda s, c, t, e, start, end: rows)
        r = _call_tool("read_range", {"concept_ids": [7],
                                      "entity_type": "country",
                                      "entity_id": 156,
                                      "start": "1900-01-01", "end": "2020-12-31"})
        assert len(r.structured_content["7"]) == 120  # bare map, no envelope
        assert _clean_meta(r.meta)["rows_returned"] == 120

    def test_fully_empty_range_meta_zero(self, fake_session, monkeypatch):
        monkeypatch.setattr(cache_mod, "read_cache_range",
                            lambda s, c, t, e, start, end: [])
        monkeypatch.setattr(dispatch_mod, "read_range",
                            lambda s, c, t, e, start, end: {})
        r = _call_tool("read_range", {"concept_ids": [9],
                                      "entity_type": "country",
                                      "entity_id": 156,
                                      "start": "2024-01-01", "end": "2024-12-31"})
        assert r.structured_content == {"9": []}
        assert _clean_meta(r.meta)["rows_returned"] == 0

    def test_in_process_call_returns_bare_map(self, golden_reads):
        out = biz.read_range([7, 8, 9], "country", 156,
                             "2020-01-01", "2021-12-31")
        assert out == {7: json.loads(READ_RANGE_GOLDEN_TEXT)["7"],
                       8: json.loads(READ_RANGE_GOLDEN_TEXT)["8"],
                       9: []}  # int keys, bare dict — no envelope


# --- middleware row-count derivation (unit) --------------------------------------

class TestCountReadRows:
    def test_read_counts_only_served_observations(self):
        structured = {"result": [
            {"date": "d1", "value": 1.0, "brand": "finddata"},
            {"date": "d2", "value": None, "status": "unavailable"},
        ]}
        assert biz._count_read_rows("read", structured) == 1

    def test_read_range_sums_map_lists(self):
        assert biz._count_read_rows("read_range", {"7": [{}, {}], "8": []}) == 2

    def test_unknown_shape_left_unstamped(self):
        assert biz._count_read_rows("read", {"unexpected": 1}) is None
        assert biz._count_read_rows("read_range", []) is None
        assert biz._count_read_rows("read", None) is None


# --- domain read tools: additive in-envelope stamp (unchanged design) -----------

class TestYearbookReadContract:
    ROWS = [{"data_year": y, "region": "全国", "value_num": float(y),
             "value": None, "unit": "万人"} for y in (2022, 2023)]

    def test_rows_returned_equals_count(self, monkeypatch):
        monkeypatch.setattr(ty, "domain_query", FakeDomain([self.ROWS]))
        r = biz.yearbook_read(1, "全国", 2022, 2023)
        assert r["rows_returned"] == len(r["results"]) == r["count"] == 2
        assert {"results", "count", "truncated"} <= set(r)  # existing keys intact

    def test_empty_hit_is_zero(self, monkeypatch):
        monkeypatch.setattr(ty, "domain_query", FakeDomain([[]]))
        r = biz.yearbook_read(1, "无此地区")
        assert r["rows_returned"] == 0 and r["results"] == []

    def test_envelope_tool_not_double_stamped_in_meta(self, monkeypatch):
        """Envelope tools carry rows_returned in-payload, not in result._meta."""
        monkeypatch.setattr(ty, "domain_query", FakeDomain([self.ROWS]))
        r = _call_tool("yearbook_read", {"indicator_id": 1, "region": "全国",
                                         "start_year": 2022, "end_year": 2023})
        assert r.structured_content["rows_returned"] == 2
        assert "rows_returned" not in _clean_meta(r.meta)


class TestWbReadContract:
    def test_rows_returned_equals_count(self, monkeypatch):
        obs = [{"year": y, "country_code": "CHN", "country_name": "中国",
                "value": float(y), "unit": "USD"} for y in (2022, 2023)]
        fake = FakeDomain([[{"code": "CHN", "name_cn": "中国"}], obs])
        monkeypatch.setattr(tw, "domain_query", fake)
        r = biz.wb_read("NY.GDP.PCAP.CD", ["中国"], 2022, 2023)
        assert r["rows_returned"] == len(r["results"]) == r["count"] == 2

    def test_empty_hit_is_zero(self, monkeypatch):
        fake = FakeDomain([[{"code": "CHN", "name_cn": "中国"}], []])
        monkeypatch.setattr(tw, "domain_query", fake)
        r = biz.wb_read("NY.GDP.PCAP.CD", ["CHN"])
        assert r["rows_returned"] == 0 and r["results"] == []

    def test_domain_unavailable_passes_through_unstamped(self, monkeypatch):
        fail = {"status": "domain_unavailable", "detail": "OperationalError"}
        monkeypatch.setattr(tw, "domain_query", FakeDomain(fail))
        r = biz.wb_read("X", ["CHN"])
        assert r == fail  # not a success: no rows_returned


class TestGtaReadContract:
    _VAR = [{"name": "assr", "label_cn": "总资产", "table_name": "panel_c0"}]

    def test_rows_returned_equals_count(self, monkeypatch):
        obs = [{"firm_id": "000002", "firm": "示例公司", "year": y, "value": 1.5}
               for y in (2022, 2023)]
        monkeypatch.setattr(tg, "domain_query", FakeDomain([self._VAR, obs]))
        r = biz.gta_read("assr", 2022, 2023)
        assert r["rows_returned"] == len(r["results"]) == r["count"] == 2

    def test_empty_hit_is_zero(self, monkeypatch):
        monkeypatch.setattr(tg, "domain_query", FakeDomain([self._VAR, []]))
        r = biz.gta_read("assr", 2999, 2999)
        assert r["rows_returned"] == 0 and r["results"] == []


class TestCityReadContract:
    _VAR = [{"var_id": "v001"}]

    def test_rows_returned_equals_count(self, monkeypatch):
        obs = [{"year": y, "city_code": 1101, "city_name": "北京市",
                "value": float(y)} for y in (2022, 2023)]
        monkeypatch.setattr(tc, "domain_query", FakeDomain([self._VAR, obs]))
        r = biz.city_read("v001", "北京市", 2022, 2023)
        assert r["rows_returned"] == len(r["results"]) == r["count"] == 2

    def test_empty_hit_is_zero(self, monkeypatch):
        monkeypatch.setattr(tc, "domain_query", FakeDomain([self._VAR, []]))
        r = biz.city_read("v001")
        assert r["rows_returned"] == 0 and r["results"] == []


class TestIa35RegCountsReadContract:
    ROWS = [{"year": y, "province": "北京市", "city": None, "county": None,
             "industry": "制造业", "industry_code": "C", "new_firms": 10 + y,
             "bad_total": 0} for y in (2022, 2023)]

    def test_rows_returned_equals_count(self, monkeypatch):
        monkeypatch.setattr(tf, "query", FakeDomain([self.ROWS]))
        r = biz.ia35_reg_counts_read(2022, 2023, "北京市")
        assert r["rows_returned"] == len(r["results"]) == r["count"] == 2
        assert r["results"][0]["brand"] == "finddata"

    def test_empty_hit_is_zero(self, monkeypatch):
        monkeypatch.setattr(tf, "query", FakeDomain([[]]))
        r = biz.ia35_reg_counts_read(1900, 1901)
        assert r["rows_returned"] == 0 and r["results"] == []


# --- registration surface --------------------------------------------------------

def _registered_tools(mcp):
    """FastMCP version-agnostic tool listing (3.x/4.x: async list_tools)."""
    import asyncio
    try:
        tools = mcp.list_tools()
    except AttributeError:  # fastmcp < 2.7
        return mcp._tool_manager.list_tools()
    if hasattr(tools, "__await__"):
        tools = asyncio.run(tools)
    return tools


def test_all_observation_read_tools_are_registered():
    """The stamped tools are exactly the server's registered read surface —
    nothing serving observations was missed."""
    names = {t.name for t in _registered_tools(biz.mcp)}
    for name in STAMPED_READ_TOOLS:
        assert name in names, name
