"""Tests for the yearbook federation tools (tools_yearbook.py).

Unit tests fake the domain query — no network. Integration tests hit the
real read-only PG over the tailnet via FDBIZ_DOMAIN_PG_URL, which is
auto-loaded from the repo .env at import time (before skipif markers run).
"""
from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

import fd_find_data_business_mcp.tools_yearbook as ty

# Load the repo .env before the skipif markers evaluate.
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
    """Stand-in for domain.query that records calls and returns canned rows."""

    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, dbname, sql, params=None):
        self.calls.append((dbname, sql, params or {}))
        return self.rows


def _ind_row(id_, name, category="综合", unit="亿元"):
    return {"id": id_, "name": name, "category": category, "unit": unit}


def _val_row(year, region="全国", value_num=Decimal("1.5"), value="1.5", unit="亿元"):
    return {"data_year": year, "region": region, "value_num": value_num,
            "value": value, "unit": unit}


# --- Unit: yearbook_search_indicators ----------------------------------------

class TestSearchUnit:
    def test_blank_query_returns_empty_envelope_without_hitting_domain(self, monkeypatch):
        fake = FakeDomain([])
        monkeypatch.setattr(ty, "domain_query", fake)
        for q in ("", "   ", "\t\n"):
            r = ty.yearbook_search_indicators(q)
            assert r == {"results": [], "count": 0, "truncated": False}
        assert fake.calls == []

    def test_rows_carry_brand_and_no_forbidden_keys(self, monkeypatch):
        leaky = _ind_row(1, "生产总值")
        leaky.update({"table_id": 9, "source": "upstream", "real_source_used": "x"})
        monkeypatch.setattr(ty, "domain_query", FakeDomain([leaky]))
        r = ty.yearbook_search_indicators("生产总值")
        assert r["count"] == 1
        row = r["results"][0]
        assert row["brand"] == "finddata"
        assert row == {"id": 1, "name": "生产总值", "category": "综合",
                       "unit": "亿元", "brand": "finddata"}
        assert not any(k in row for k in _FORBIDDEN)

    def test_truncation_flag_true_beyond_limit(self, monkeypatch):
        rows = [_ind_row(i, f"指标{i}") for i in range(51)]  # default limit 50
        monkeypatch.setattr(ty, "domain_query", FakeDomain(rows))
        r = ty.yearbook_search_indicators("指标")
        assert r["count"] == 50 and len(r["results"]) == 50
        assert r["truncated"] is True

    def test_truncation_flag_false_within_limit(self, monkeypatch):
        rows = [_ind_row(i, f"指标{i}") for i in range(10)]
        monkeypatch.setattr(ty, "domain_query", FakeDomain(rows))
        r = ty.yearbook_search_indicators("指标")
        assert r["count"] == 10 and r["truncated"] is False

    def test_limit_capped_at_200(self, monkeypatch):
        fake = FakeDomain([])
        monkeypatch.setattr(ty, "domain_query", fake)
        ty.yearbook_search_indicators("x", limit=100_000)
        assert fake.calls[0][2]["lim"] == 201
        ty.yearbook_search_indicators("x", limit=0)
        assert fake.calls[1][2]["lim"] == 2  # floor of 1 + truncation probe

    def test_ranking_sql_is_exact_then_prefix_then_position(self, monkeypatch):
        fake = FakeDomain([])
        monkeypatch.setattr(ty, "domain_query", fake)
        ty.yearbook_search_indicators("生产总值")
        sql = fake.calls[0][1]
        i_exact = sql.index("lower(ind.std_name) = lower(:q)")
        i_prefix = sql.index("ind.std_name ILIKE :pfx")
        i_pos = sql.index("position(lower(:q) IN lower(ind.std_name))")
        assert i_exact < i_prefix < i_pos
        assert sql.index("DESC", i_exact) < i_prefix  # exact sorts DESC first
        assert " ILIKE :pat" in sql  # contains-match filter

    def test_like_wildcards_in_user_input_are_escaped(self, monkeypatch):
        fake = FakeDomain([])
        monkeypatch.setattr(ty, "domain_query", fake)
        ty.yearbook_search_indicators("100%_a")
        p = fake.calls[0][2]
        assert p["pat"] == "%100\\%\\_a%"
        assert p["pfx"] == "100\\%\\_a%"
        assert p["q"] == "100%_a"

    def test_search_considers_aliases_with_null_indicator_guarded(self, monkeypatch):
        fake = FakeDomain([_ind_row(454, "全省生产总值")])
        monkeypatch.setattr(ty, "domain_query", fake)
        r = ty.yearbook_search_indicators("全省生产总值")
        assert r["count"] == 1  # alias-matched indicator is served
        sql = fake.calls[0][1]
        assert "indicator_aliases" in sql
        assert "indicator_id IS NOT NULL" in sql

    def test_domain_unavailable_passes_through_unchanged(self, monkeypatch):
        fail = {"status": "domain_unavailable", "detail": "OperationalError"}
        monkeypatch.setattr(ty, "domain_query", FakeDomain(fail))
        assert ty.yearbook_search_indicators("生产总值") == fail


# --- Unit: yearbook_read ------------------------------------------------------

class TestReadUnit:
    def test_envelope_rows_brand_and_coercion(self, monkeypatch):
        rows = [
            _val_row(2020, value_num=Decimal("30406.0"), value="30406"),
            _val_row(2019, value_num=None, value="Linxiang City", unit=None),
        ]
        monkeypatch.setattr(ty, "domain_query", FakeDomain(rows))
        r = ty.yearbook_read(1, region="全国")
        assert r["count"] == 2 and r["truncated"] is False
        first, second = r["results"]
        assert first["year"] == 2019 and first["value"] == "Linxiang City"
        assert second == {"year": 2020, "region": "全国",
                          "value": 30406.0, "unit": "亿元", "brand": "finddata"}
        assert not any(k in first or k in second for k in _FORBIDDEN)

    def test_rows_sorted_by_year_even_when_sql_orders_by_region(self, monkeypatch):
        rows = [_val_row(2022, region="浙江省"), _val_row(1999, region="北京市"),
                _val_row(2010, region="上海市")]
        monkeypatch.setattr(ty, "domain_query", FakeDomain(rows))
        r = ty.yearbook_read(1)
        years = [row["year"] for row in r["results"]]
        assert years == sorted(years) == [1999, 2010, 2022]

    def test_sql_is_distinct_on_latest_edition_and_index_shaped(self, monkeypatch):
        fake = FakeDomain([])
        monkeypatch.setattr(ty, "domain_query", fake)
        ty.yearbook_read(7, region="浙江省", start_year=2000, end_year=2020)
        dbname, sql, params = fake.calls[0]
        assert dbname == "yearbook_catalog"
        assert "DISTINCT ON (region, data_year)" in sql
        assert "edition_year DESC NULLS LAST" in sql
        assert "indicator_id = :iid" in sql
        assert "(region = :region OR ((region IS NULL OR btrim(region) = '')" in sql
        assert "dims->>'admin_region' = :region))" in sql
        assert "data_year BETWEEN :sy AND :ey" in sql
        assert "table_id" not in sql and "table_cells" not in sql
        assert params == {"iid": 7, "region": "浙江省", "sy": 2000, "ey": 2020, "lim": 201}

    def test_sql_numeric_row_wins_edition_ties(self, monkeypatch):
        fake = FakeDomain([])
        monkeypatch.setattr(ty, "domain_query", fake)
        ty.yearbook_read(7)
        sql = fake.calls[0][1]
        i_edition = sql.index("edition_year DESC NULLS LAST")
        i_numeric = sql.index("(value_num IS NULL)")
        i_id = sql.index(", id", i_numeric)
        assert i_edition < i_numeric < i_id  # numeric rows win ties, id breaks the rest

    def test_no_region_argument_means_no_region_filter(self, monkeypatch):
        fake = FakeDomain([])
        monkeypatch.setattr(ty, "domain_query", fake)
        ty.yearbook_read(7)  # no region filter at all
        sql = fake.calls[0][1]
        assert "admin_region" not in sql
        assert "region" not in fake.calls[0][2]

    def test_open_ended_year_bounds(self, monkeypatch):
        fake = FakeDomain([])
        monkeypatch.setattr(ty, "domain_query", fake)
        ty.yearbook_read(7, start_year=2010)
        assert fake.calls[0][2]["sy"] == 2010
        assert fake.calls[0][2]["ey"] == 2147483647
        ty.yearbook_read(7, end_year=2010)
        assert fake.calls[1][2]["sy"] == -2147483648 and fake.calls[1][2]["ey"] == 2010

    def test_limit_capped_at_1000(self, monkeypatch):
        fake = FakeDomain([])
        monkeypatch.setattr(ty, "domain_query", fake)
        ty.yearbook_read(1, limit=99_999)
        assert fake.calls[0][2]["lim"] == 1001
        ty.yearbook_read(1, limit=-5)
        assert fake.calls[1][2]["lim"] == 2

    def test_no_rows_is_truthful_empty_not_an_error(self, monkeypatch):
        monkeypatch.setattr(ty, "domain_query", FakeDomain([]))
        r = ty.yearbook_read(12345, region="无此区域")
        assert r == {"results": [], "count": 0, "truncated": False}
        assert "status" not in r

    def test_domain_unavailable_passes_through_unchanged(self, monkeypatch):
        fail = {"status": "domain_unavailable", "detail": "TimeoutError"}
        monkeypatch.setattr(ty, "domain_query", FakeDomain(fail))
        assert ty.yearbook_read(1) == fail


# --- Integration: real PG over the tailnet ------------------------------------

@needs_pg
class TestSearchIntegration:
    def test_common_chinese_term_finds_indicators_with_unit(self):
        r = ty.yearbook_search_indicators("生产总值")
        assert r["count"] > 0
        assert all("unit" in row for row in r["results"])
        assert all(row["brand"] == "finddata" for row in r["results"])

    def test_exact_match_ranks_first(self):
        r = ty.yearbook_search_indicators("生产总值")
        assert r["results"][0]["name"] == "生产总值"

    def test_small_limit_reports_truncation(self):
        r = ty.yearbook_search_indicators("生产总值", limit=3)
        assert r["count"] == 3 and r["truncated"] is True


def _discover_series():
    """Find a real (indicator, region) pair using index-shaped queries only.

    The obvious GROUP BY over indicator_values is a 24M-row full aggregate —
    the server-side statement_timeout cancels it on a cold cache (correctly:
    it violates the query-shape discipline this federation is built on).
    Instead: pick an indicator from the small indicators table via the same
    search the tool uses, then probe likely regions with indexed point
    lookups until one has rows.
    """
    hits = ty.yearbook_search_indicators("生产总值", limit=5)
    assert isinstance(hits.get("results"), list) and hits["results"], hits
    probe_regions = ["北京市", "浙江省", "上海市", "广东省", "天津市", "全国"]
    for iid in [r["id"] for r in hits["results"]]:
        for region in probe_regions:
            rows = ty.domain_query("yearbook_catalog", """
                SELECT min(data_year) AS ymin, max(data_year) AS ymax
                FROM indicator_values
                WHERE indicator_id = :iid AND region = :region
            """, {"iid": iid, "region": region})
            if isinstance(rows, list) and rows and rows[0]["ymin"] is not None:
                return iid, region, rows[0]["ymin"], rows[0]["ymax"]
    raise AssertionError(f"no series found via index-shaped probes: {hits}")


@needs_pg
class TestReadIntegration:
    def test_real_indicator_series(self):
        iid, region, ymin, ymax = _discover_series()
        r = ty.yearbook_read(iid, region=region, start_year=ymin, end_year=ymax)
        assert r["count"] > 0
        years = [row["year"] for row in r["results"]]
        assert years == sorted(years), "rows must be ordered by year"
        assert all(ymin <= y <= ymax for y in years)
        assert all(row["region"] == region for row in r["results"])
        assert all(row["brand"] == "finddata" for row in r["results"])
        assert not any(k in row for row in r["results"] for k in _FORBIDDEN)

    def test_unfiltered_years_still_works(self):
        iid, region, _, _ = _discover_series()
        r = ty.yearbook_read(iid, region=region)
        assert 0 < r["count"] <= 200
        # small limit over the same series must report truthful truncation
        small = ty.yearbook_read(iid, region=region, limit=5)
        assert small["count"] == 5 and small["truncated"] is True

    def test_filter_matching_nothing_returns_empty_envelope(self):
        iid, _, _, _ = _discover_series()
        r = ty.yearbook_read(iid, region="__无此区域__", start_year=1800, end_year=1801)
        assert r == {"results": [], "count": 0, "truncated": False}
        assert "status" not in r

    def test_regression_linxiang_numeric_value_over_annotation(self):
        # Real regression (2026-09-20): this cell used to serve "Linxiang
        # City" — the English annotation sibling — non-deterministically.
        # The source table flattens one row into an annotation + total +
        # sub-measures; the fix pins the first numeric sibling (the source
        # row's total, 346.19 亿) deterministically. 3461901.12 =
        # 427614.71 + 1400621.82 + 1633664.59 (一/二/三产), a consistency
        # anchor that this is the 合计 row.
        r = ty.yearbook_read(12209, region="临湘市", start_year=2023, end_year=2023)
        assert r["count"] == 1, r
        assert r["results"][0]["value"] == pytest.approx(3461901.11748462)

    def test_province_series_via_dimension_guard(self):
        # 广东's GDP series lives with an EMPTY region column and the
        # province in dims.admin_region. Anchors: 2023=135673.2,
        # 2024=141633.8 (亿元).
        r = ty.yearbook_read(12209, region="广东", start_year=2023, end_year=2024)
        assert r["count"] == 2, r
        vals = {row["year"]: row["value"] for row in r["results"]}
        assert vals[2023] == pytest.approx(135673.2)
        assert vals[2024] == pytest.approx(141633.8)

    def test_conflicting_column_region_not_overridden_by_dimension(self):
        # Cross-region comparison tables keep ANOTHER region in the column
        # (e.g. region='江苏' with dims.admin_region='广东'): the guard must
        # never serve those rows for a 广东 query.
        r = ty.yearbook_read(12209, region="广东", start_year=2004, end_year=2008)
        assert isinstance(r.get("results"), list), r
        for row in r["results"]:
            assert (row["region"] or "").strip() in ("", "广东"), row

    def test_read_query_plan_is_index_shaped(self):
        iid, region, ymin, ymax = _discover_series()
        rows = ty.domain_query("yearbook_catalog", """
            EXPLAIN SELECT DISTINCT ON (region, data_year)
                   data_year, region, value_num, value, unit
            FROM indicator_values
            WHERE indicator_id = :iid
              AND (region = :region OR ((region IS NULL OR btrim(region) = '')
                   AND dims->>'admin_region' = :region))
              AND data_year BETWEEN :sy AND :ey
            ORDER BY region, data_year, edition_year DESC NULLS LAST,
                     (value_num IS NULL), id
        """, {"iid": iid, "region": region, "sy": ymin, "ey": ymax})
        assert isinstance(rows, list), rows
        plan = "\n".join(r["QUERY PLAN"] for r in rows)
        assert ("Index Scan" in plan) or ("Bitmap" in plan), plan
        assert "Seq Scan on indicator_values" not in plan, plan
