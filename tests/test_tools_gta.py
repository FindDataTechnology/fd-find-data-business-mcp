"""Tests for the GTA panel federation tools (tools_gta.py).

Unit tests fake the domain query — no network. Integration tests hit the
real read-only PG over the tailnet via FDBIZ_DOMAIN_PG_URL, which is
auto-loaded from the repo .env at import time (before skipif markers run).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import fd_find_data_business_mcp.tools_gta as tg

_ENV = Path(__file__).resolve().parents[1] / ".env"
if not os.environ.get("FDBIZ_DOMAIN_PG_URL") and _ENV.exists():
    for _line in _ENV.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

needs_pg = pytest.mark.skipif(
    not os.environ.get("FDBIZ_DOMAIN_PG_URL"), reason="needs FDBIZ_DOMAIN_PG_URL")

_FORBIDDEN = ("table_name", "sublib_code", "source", "source_used", "table_id")


class FakeDomain:
    """Stand-in for domain.query: canned rows per call, recorded calls."""

    def __init__(self, responses):
        self.responses = responses if isinstance(responses, list) else [responses]
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, dbname, sql, params=None):
        self.calls.append((dbname, sql, params or {}))
        idx = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[idx]


def _var_row(code="b001101000", label="营业收入", table="panel_c1"):
    return {"name": code, "label_cn": label, "table_name": table}


def _search_row(code="b001101000", label="营业收入"):
    """Shape of the search projection (SELECT v.name AS code, v.label_cn AS label)."""
    return {"code": code, "label": label}


def _val_row(firm_id=1, firm="某公司", year=2023, value=100.5):
    return {"firm_id": firm_id, "firm": firm, "year": year, "value": value}


# --- Unit: gta_search_variables ----------------------------------------------

class TestSearchUnit:
    def test_blank_query_returns_empty_envelope_without_hitting_domain(self, monkeypatch):
        fake = FakeDomain([])
        monkeypatch.setattr(tg, "domain_query", fake)
        assert tg.gta_search_variables("  ") == {"results": [], "count": 0, "truncated": False}
        assert fake.calls == []

    def test_rows_carry_brand_and_storage_identity_is_stripped(self, monkeypatch):
        leaky = _search_row()
        leaky.update({"sublib_code": "FIN", "dict_no": 12})
        monkeypatch.setattr(tg, "domain_query", FakeDomain([[leaky]]))
        r = tg.gta_search_variables("营业收入")
        row = r["results"][0]
        assert row == {"code": "b001101000", "label": "营业收入", "brand": "finddata"}
        assert not any(k in row for k in _FORBIDDEN)

    def test_truncation_flag(self, monkeypatch):
        rows = [_search_row(code=f"v{i}") for i in range(6)]
        monkeypatch.setattr(tg, "domain_query", FakeDomain([rows]))
        r = tg.gta_search_variables("x", limit=5)
        assert r["count"] == 5 and r["truncated"] is True

    def test_domain_unavailable_passes_through(self, monkeypatch):
        fail = {"status": "domain_unavailable", "detail": "OperationalError"}
        monkeypatch.setattr(tg, "domain_query", FakeDomain(fail))
        assert tg.gta_search_variables("x") == fail


# --- Unit: gta_read -----------------------------------------------------------

class TestReadUnit:
    def test_malformed_name_is_unknown_without_domain_hit(self, monkeypatch):
        fake = FakeDomain([])
        monkeypatch.setattr(tg, "domain_query", fake)
        for name in ("", "  ", "bad name; DROP TABLE x", "*"):
            r = tg.gta_read(name)
            assert r["status"] == "unknown_variable"
        assert fake.calls == []

    def test_variable_not_in_catalog_is_unknown(self, monkeypatch):
        fake = FakeDomain([[]])
        monkeypatch.setattr(tg, "domain_query", fake)
        assert tg.gta_read("nope_123") == {"status": "unknown_variable", "variable": "nope_123"}

    def test_unexpected_storage_table_is_refused(self, monkeypatch):
        fake = FakeDomain([[_var_row(table="panel_evil")]])
        monkeypatch.setattr(tg, "domain_query", fake)
        r = tg.gta_read("b001101000")
        assert r["status"] == "unknown_variable"
        assert len(fake.calls) == 1  # catalog lookup only; nothing interpolated

    def test_c0_variable_reads_directly_without_join(self, monkeypatch):
        fake = FakeDomain([[_var_row(code="equitynature_p", table="panel_c0")],
                           [_val_row()]])
        monkeypatch.setattr(tg, "domain_query", fake)
        r = tg.gta_read("equitynature_p", start_year=2020, end_year=2023)
        sql = fake.calls[1][1]
        assert "FROM panel_c0" in sql and "JOIN" not in sql
        assert "c.equitynature_p IS NOT NULL" in sql
        assert fake.calls[1][2]["sy"] == 2020 and fake.calls[1][2]["ey"] == 2023
        assert "firms" not in fake.calls[1][2]

    def test_child_table_variable_joins_identity_table(self, monkeypatch):
        fake = FakeDomain([[_var_row()], [_val_row()]])
        monkeypatch.setattr(tg, "domain_query", fake)
        tg.gta_read("b001101000", firm_ids=[5, 5, 7])
        sql = fake.calls[1][1]
        assert "FROM panel_c1 t" in sql
        assert "JOIN panel_c0" in sql
        assert "AND c.id_org = ANY(:firms)" in sql
        # id_org is zero-padded text: short codes normalize to the 6-digit key.
        assert fake.calls[1][2]["firms"] == ["000005", "000007"]

    def test_firm_ids_accept_stored_and_short_forms(self, monkeypatch):
        fake = FakeDomain([[_var_row()], []])
        monkeypatch.setattr(tg, "domain_query", fake)
        tg.gta_read("b001101000", firm_ids=["000002", "2", 2, "  "])
        assert fake.calls[1][2]["firms"] == ["000002"]  # padded, deduped, blank dropped

    def test_envelope_brand_coercion_and_truncation(self, monkeypatch):
        rows = [_val_row(year=2020 + i) for i in range(4)]
        fake = FakeDomain([[_var_row()], rows])
        monkeypatch.setattr(tg, "domain_query", fake)
        r = tg.gta_read("b001101000", limit=3)
        assert r["count"] == 3 and r["truncated"] is True
        row = r["results"][0]
        assert row == {"firm_id": 1, "firm": "某公司", "year": 2020,
                       "value": 100.5, "brand": "finddata"}
        assert not any(k in row for k in _FORBIDDEN)

    def test_domain_failure_in_either_step_passes_through(self, monkeypatch):
        fail = {"status": "domain_unavailable", "detail": "TimeoutError"}
        monkeypatch.setattr(tg, "domain_query", FakeDomain([fail]))
        assert tg.gta_read("b001101000") == fail
        monkeypatch.setattr(tg, "domain_query", FakeDomain([[_var_row()], fail]))
        assert tg.gta_read("b001101000") == fail


# --- Integration: real PG over the tailnet ------------------------------------

@needs_pg
class TestSearchIntegration:
    def test_research_expense_label_hits_known_code(self):
        r = tg.gta_search_variables("研发费用")
        assert any(row["code"] == "b001216000" for row in r["results"]), r

    def test_revenue_label_hits_known_code(self):
        r = tg.gta_search_variables("营业收入")
        assert any(row["code"] == "b001101000" for row in r["results"]), r


@needs_pg
class TestReadIntegration:
    def test_revenue_firm_years(self):
        r = tg.gta_read("b001101000", start_year=2022, end_year=2023, limit=5)
        assert r["count"] == 5 and r["truncated"] is True
        for row in r["results"]:
            assert 2022 <= row["year"] <= 2023
            assert row["firm"] and isinstance(row["value"], (int, float))
            assert row["brand"] == "finddata"

    def test_firm_filter_returns_only_requested_firms(self):
        probe = tg.gta_read("b001101000", start_year=2023, end_year=2023, limit=3)
        fid = probe["results"][0]["firm_id"]
        r = tg.gta_read("b001101000", start_year=2023, end_year=2023, firm_ids=[fid])
        assert r["count"] >= 1
        assert all(row["firm_id"] == fid for row in r["results"])

    def test_unknown_variable_is_reported(self):
        assert tg.gta_read("__no_such_variable__")["status"] == "unknown_variable"
