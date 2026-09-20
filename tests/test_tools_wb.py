"""Tests for the World Bank federation tools (tools_wb.py).

Unit tests fake the domain query — no network. Integration tests hit the
real read-only PG over the tailnet via FDBIZ_DOMAIN_PG_URL, which is
auto-loaded from the repo .env at import time (before skipif markers run).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import fd_find_data_business_mcp.tools_wb as tw

_ENV = Path(__file__).resolve().parents[1] / ".env"
if not os.environ.get("FDBIZ_DOMAIN_PG_URL") and _ENV.exists():
    for _line in _ENV.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

needs_pg = pytest.mark.skipif(
    not os.environ.get("FDBIZ_DOMAIN_PG_URL"), reason="needs FDBIZ_DOMAIN_PG_URL")

_FORBIDDEN = ("source", "source_used", "real_source_used", "real_source", "table_id")


class FakeDomain:
    """Stand-in for domain.query: canned rows per call, recorded calls."""

    def __init__(self, responses):
        # responses: list of return values consumed in call order, or a
        # single return value reused for every call.
        self.responses = responses if isinstance(responses, list) else [responses]
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, dbname, sql, params=None):
        self.calls.append((dbname, sql, params or {}))
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[idx]


def _ind_row(code="NY.GDP.PCAP.CD", name="人均GDP", name_en="GDP per capita"):
    return {"code": code, "name": name, "name_en": name_en,
            "topic": "Economic Policy & Debt", "unit": "USD"}


def _country_row(code="CHN", name_cn="中国"):
    return {"code": code, "name_cn": name_cn}


def _obs_row(year=2020, code="CHN", value=10408.7, name="中国", unit="USD"):
    return {"year": year, "country_code": code, "value": value,
            "country_name": name, "unit": unit}


# --- Unit: wb_search_indicators ----------------------------------------------

class TestSearchUnit:
    def test_blank_query_returns_empty_envelope_without_hitting_domain(self, monkeypatch):
        fake = FakeDomain([])
        monkeypatch.setattr(tw, "domain_query", fake)
        for q in ("", "   "):
            assert tw.wb_search_indicators(q) == {"results": [], "count": 0, "truncated": False}
        assert fake.calls == []

    def test_rows_carry_brand_and_no_forbidden_keys(self, monkeypatch):
        leaky = _ind_row()
        leaky.update({"source": "upstream", "table_id": 9})
        monkeypatch.setattr(tw, "domain_query", FakeDomain([[leaky]]))
        r = tw.wb_search_indicators("人均GDP")
        row = r["results"][0]
        assert row["brand"] == "finddata"
        assert not any(k in row for k in _FORBIDDEN)

    def test_truncation_flag(self, monkeypatch):
        rows = [_ind_row(code=f"X.{i}") for i in range(6)]
        monkeypatch.setattr(tw, "domain_query", FakeDomain([rows]))
        r = tw.wb_search_indicators("x", limit=5)
        assert r["count"] == 5 and r["truncated"] is True

    def test_like_wildcards_escaped(self, monkeypatch):
        fake = FakeDomain([[]])
        monkeypatch.setattr(tw, "domain_query", fake)
        tw.wb_search_indicators("a%b_c")
        assert fake.calls[0][2]["pat"] == "%a\\%b\\_c%"

    def test_domain_unavailable_passes_through(self, monkeypatch):
        fail = {"status": "domain_unavailable", "detail": "OperationalError"}
        monkeypatch.setattr(tw, "domain_query", FakeDomain(fail))
        assert tw.wb_search_indicators("gdp") == fail


# --- Unit: wb_read ------------------------------------------------------------

class TestReadUnit:
    def test_missing_code_or_countries_returns_empty_without_domain_hit(self, monkeypatch):
        fake = FakeDomain([])
        monkeypatch.setattr(tw, "domain_query", fake)
        assert tw.wb_read("", ["CHN"]) == {"results": [], "count": 0, "truncated": False}
        assert tw.wb_read("NY.GDP.PCAP.CD", []) == {"results": [], "count": 0, "truncated": False}
        assert fake.calls == []

    def test_countries_resolve_then_point_read(self, monkeypatch):
        fake = FakeDomain([[  _country_row("CHN"), _country_row("USA")], [_obs_row()]])
        monkeypatch.setattr(tw, "domain_query", fake)
        r = tw.wb_read("NY.GDP.PCAP.CD", ["中国", "CHN", "USA"], start_year=2020, end_year=2020)
        assert r["count"] == 1
        row = r["results"][0]
        assert row == {"year": 2020, "country": "CHN", "country_name": "中国",
                       "value": 10408.7, "unit": "USD", "brand": "finddata"}
        assert not any(k in row for k in _FORBIDDEN)
        # resolution call then read call
        res_sql = fake.calls[0][1]
        read_sql = fake.calls[1][1]
        assert "country" in res_sql and fake.calls[0][2]["raw"] == ["中国", "CHN", "USA"]
        assert "observation" in read_sql
        assert "indicator_code = :icode" in read_sql
        assert "country_code = ANY(:ccodes)" in read_sql
        assert fake.calls[1][2]["ccodes"] == ["CHN", "USA"]  # deduped + sorted
        assert fake.calls[1][2]["lim"] == 601

    def test_unresolved_countries_empty_envelope_without_read(self, monkeypatch):
        fake = FakeDomain([[]])
        monkeypatch.setattr(tw, "domain_query", fake)
        r = tw.wb_read("NY.GDP.PCAP.CD", ["不存在国"])
        assert r == {"results": [], "count": 0, "truncated": False}
        assert len(fake.calls) == 1  # resolution only

    def test_truncation_flag(self, monkeypatch):
        rows = [_obs_row(year=1960 + i) for i in range(4)]
        fake = FakeDomain([[_country_row()], rows])
        monkeypatch.setattr(tw, "domain_query", fake)
        r = tw.wb_read("NY.GDP.PCAP.CD", ["CHN"], limit=3)
        assert r["count"] == 3 and r["truncated"] is True

    def test_domain_failure_in_either_step_passes_through(self, monkeypatch):
        fail = {"status": "domain_unavailable", "detail": "TimeoutError"}
        monkeypatch.setattr(tw, "domain_query", FakeDomain([fail]))
        assert tw.wb_read("X", ["CHN"]) == fail
        monkeypatch.setattr(tw, "domain_query", FakeDomain([[_country_row()], fail]))
        assert tw.wb_read("X", ["CHN"]) == fail


# --- Integration: real PG over the tailnet ------------------------------------

@needs_pg
class TestSearchIntegration:
    def test_fuzzy_chinese_finds_indicators_with_unit(self):
        r = tw.wb_search_indicators("人均")
        assert r["count"] > 0
        assert all(row["brand"] == "finddata" for row in r["results"])

    def test_exact_code_ranks_first(self):
        r = tw.wb_search_indicators("NY.GDP.PCAP.CD")
        assert r["results"][0]["code"] == "NY.GDP.PCAP.CD"


@needs_pg
class TestReadIntegration:
    def test_china_gdp_per_capita_series(self):
        r = tw.wb_read("NY.GDP.PCAP.CD", ["CHN"], start_year=1960, end_year=2025)
        assert r["count"] > 50, r
        years = [row["year"] for row in r["results"]]
        assert years == sorted(years)
        assert all(row["country"] == "CHN" for row in r["results"])
        assert all(isinstance(row["value"], (int, float)) for row in r["results"])

    def test_chinese_name_resolves_like_iso3(self):
        by_code = tw.wb_read("NY.GDP.PCAP.CD", ["CHN"], start_year=2024, end_year=2024)
        by_name = tw.wb_read("NY.GDP.PCAP.CD", ["中国"], start_year=2024, end_year=2024)
        assert by_code["results"] == by_name["results"]
        assert by_code["count"] == 1

    def test_unknown_indicator_is_truthful_empty(self):
        r = tw.wb_read("NOPE.NOPE.NOPE", ["CHN"], start_year=2000, end_year=2001)
        assert r == {"results": [], "count": 0, "truncated": False}

    def test_unknown_country_is_truthful_empty(self):
        r = tw.wb_read("NY.GDP.PCAP.CD", ["__无此国__"])
        assert r == {"results": [], "count": 0, "truncated": False}

    def test_truncation_marks_bounded_multi_country_read(self):
        r = tw.wb_read("NY.GDP.PCAP.CD", ["CHN", "USA", "JPN"], limit=5)
        assert r["count"] == 5 and r["truncated"] is True
