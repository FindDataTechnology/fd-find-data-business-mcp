"""Tests for the city panel federation tools (tools_city.py).

Unit tests fake the domain query — no network. Integration tests hit the
real read-only PG over the tailnet via FDBIZ_DOMAIN_PG_URL, which is
auto-loaded from the repo .env at import time (before skipif markers run).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import fd_find_data_business_mcp.tools_city as tc

_ENV = Path(__file__).resolve().parents[1] / ".env"
if not os.environ.get("FDBIZ_DOMAIN_PG_URL") and _ENV.exists():
    for _line in _ENV.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

needs_pg = pytest.mark.skipif(
    not os.environ.get("FDBIZ_DOMAIN_PG_URL"), reason="needs FDBIZ_DOMAIN_PG_URL")

_FORBIDDEN = ("source", "source_used", "table_id", "col_no")


class FakeDomain:
    """Stand-in for domain.query: canned rows per call, recorded calls."""

    def __init__(self, responses):
        self.responses = responses if isinstance(responses, list) else [responses]
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, dbname, sql, params=None):
        self.calls.append((dbname, sql, params or {}))
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[idx]


def _cat_row(code="v001", label="地区生产总值(万元)", unit="万元"):
    return {"code": code, "label": label, "unit": unit}


def _val_row(year=2020, city_code=110000, city_name="北京市", value=36102.6):
    return {"year": year, "city_code": city_code, "city_name": city_name,
            "value": value}


# --- Unit: city_search_variables ---------------------------------------------

class TestSearchUnit:
    def test_blank_query_returns_empty_envelope_without_hitting_domain(self, monkeypatch):
        fake = FakeDomain([])
        monkeypatch.setattr(tc, "domain_query", fake)
        assert tc.city_search_variables("") == {"results": [], "count": 0, "truncated": False}
        assert fake.calls == []

    def test_rows_carry_brand_and_no_storage_columns(self, monkeypatch):
        leaky = _cat_row()
        leaky.update({"col_no": 8, "classification": "x"})
        monkeypatch.setattr(tc, "domain_query", FakeDomain([[leaky]]))
        r = tc.city_search_variables("生产总值")
        row = r["results"][0]
        assert row == {"code": "v001", "label": "地区生产总值(万元)",
                       "unit": "万元", "brand": "finddata"}
        assert not any(k in row for k in _FORBIDDEN)

    def test_truncation_flag(self, monkeypatch):
        rows = [_cat_row(code=f"v{i:03d}") for i in range(6)]
        monkeypatch.setattr(tc, "domain_query", FakeDomain([rows]))
        r = tc.city_search_variables("x", limit=5)
        assert r["count"] == 5 and r["truncated"] is True

    def test_domain_unavailable_passes_through(self, monkeypatch):
        fail = {"status": "domain_unavailable", "detail": "OperationalError"}
        monkeypatch.setattr(tc, "domain_query", FakeDomain(fail))
        assert tc.city_search_variables("x") == fail


# --- Unit: city_read ----------------------------------------------------------

class TestReadUnit:
    def test_malformed_variable_is_unknown_without_domain_hit(self, monkeypatch):
        fake = FakeDomain([])
        monkeypatch.setattr(tc, "domain_query", fake)
        for vid in ("", "  ", "v1", "v0001", "v01; DROP TABLE x"):
            assert tc.city_read(vid)["status"] == "unknown_variable"
        assert fake.calls == []

    def test_variable_not_in_catalog_is_unknown(self, monkeypatch):
        fake = FakeDomain([[]])
        monkeypatch.setattr(tc, "domain_query", fake)
        assert tc.city_read("v999") == {"status": "unknown_variable", "variable": "v999"}

    def test_city_name_resolves_to_code_and_filters(self, monkeypatch):
        fake = FakeDomain([["known"], [{"city_code": 110000, "city_name": "北京市"}],
                           [_val_row()]])
        monkeypatch.setattr(tc, "domain_query", fake)
        r = tc.city_read("v001", city="北京市", start_year=2020, end_year=2020)
        # city_code is integer: a name must not be compared against it as text
        # (PostgreSQL cannot resolve one text parameter used for both columns).
        assert fake.calls[1][2] == {"ccode": -1, "cname": "北京市"}
        sql = fake.calls[2][1]
        assert "FROM panel_raw p" in sql and "JOIN city_dim" in sql
        assert "p.v001 IS NOT NULL" in sql
        assert "AND p.city_code = :ccode" in sql
        assert fake.calls[2][2]["ccode"] == 110000
        assert r["count"] == 1
        row = r["results"][0]
        assert row == {"year": 2020, "city_code": 110000, "city_name": "北京市",
                       "value": 36102.6, "brand": "finddata"}
        assert not any(k in row for k in _FORBIDDEN)

    def test_numeric_city_input_compares_as_code(self, monkeypatch):
        fake = FakeDomain([["known"], [{"city_code": 110000, "city_name": "北京市"}],
                           [_val_row()]])
        monkeypatch.setattr(tc, "domain_query", fake)
        tc.city_read("v001", city="110000")
        assert fake.calls[1][2] == {"ccode": 110000, "cname": "110000"}

    def test_unknown_city_name_is_empty_envelope_without_read(self, monkeypatch):
        fake = FakeDomain([["known"], []])
        monkeypatch.setattr(tc, "domain_query", fake)
        r = tc.city_read("v001", city="__无此市__")
        assert r == {"results": [], "count": 0, "truncated": False}
        assert len(fake.calls) == 2  # catalog + city resolution only

    def test_no_city_filter_omits_clause(self, monkeypatch):
        fake = FakeDomain([["known"], [_val_row()]])
        monkeypatch.setattr(tc, "domain_query", fake)
        tc.city_read("v001")
        sql = fake.calls[1][1]
        assert "ccode" not in sql and "city_code = :ccode" not in sql

    def test_domain_failure_in_either_step_passes_through(self, monkeypatch):
        fail = {"status": "domain_unavailable", "detail": "TimeoutError"}
        monkeypatch.setattr(tc, "domain_query", FakeDomain([fail]))
        assert tc.city_read("v001") == fail
        monkeypatch.setattr(tc, "domain_query", FakeDomain([["known"], fail]))
        assert tc.city_read("v001", city="北京市") == fail


# --- Integration: real PG over the tailnet ------------------------------------

@needs_pg
class TestSearchIntegration:
    def test_gdp_label_hits_v001(self):
        r = tc.city_search_variables("地区生产总值")
        assert any(row["code"] == "v001" for row in r["results"]), r


@needs_pg
class TestReadIntegration:
    def test_full_year_cross_section_of_cities(self):
        probe = tc.city_read("v001", start_year=2020, end_year=2020)
        assert probe["count"] >= 250  # 297-city panel, minus empty cells
        assert all(row["city_name"] for row in probe["results"])
        assert all(row["year"] == 2020 for row in probe["results"])

    def test_missing_cells_are_absent_not_zero(self):
        r = tc.city_read("v017", start_year=2020, end_year=2020)
        # 297 cities in 2020; 三沙市 has no value for this variable and must be
        # absent from the series rather than served as 0.
        assert r["count"] == 296
        assert all(row["city_code"] != 460300 for row in r["results"])
        assert all(row["value"] is not None for row in r["results"])

    def test_city_filter_returns_only_that_city(self):
        probe = tc.city_read("v001", start_year=2020, end_year=2020, limit=3)
        sample = probe["results"][0]
        r = tc.city_read("v001", city=sample["city_name"], start_year=2018, end_year=2022)
        assert r["count"] >= 1
        assert all(row["city_code"] == sample["city_code"] for row in r["results"])
        assert [row["year"] for row in r["results"]] == sorted(row["year"] for row in r["results"])

    def test_unknown_variable_is_reported(self):
        assert tc.city_read("v999")["status"] == "unknown_variable"
