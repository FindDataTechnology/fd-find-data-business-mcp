"""Tests for the ia35 business-registry tools (tools_firms.py).

Unit tests fake the domain query — no network. A small live tail (skipped
without FDBIZ_DOMAIN_PG_URL) exercises reg_counts normalization against
the real ia35 database; the firms_full paths live-skip while their
indexes are still building (see tools_firms module docstring).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import fd_find_data_business_mcp.tools_firms as tf

_ENV = Path(__file__).resolve().parents[1] / ".env"
if not os.environ.get("FDBIZ_DOMAIN_PG_URL") and _ENV.exists():
    for _line in _ENV.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

needs_pg = pytest.mark.skipif(
    not os.environ.get("FDBIZ_DOMAIN_PG_URL"), reason="needs FDBIZ_DOMAIN_PG_URL")

_FORBIDDEN = {
    "经营范围引用", "注册地址引用", "曾用名引用", "数据源",
    "est_date_raw", "bad_total", "bad_year",
}

_RESULT_KEYS = {
    "credit_code", "name", "legal_rep", "reg_capital", "paid_capital",
    "est_date", "est_year", "status", "company_type", "registrar",
    "province", "city", "county", "address", "business_scope",
    "former_names", "tags", "batch_year", "brand",
}

_REGCOUNT_KEYS = {
    "year", "province", "city", "county", "industry", "industry_code",
    "new_firms", "brand",
}


def _fake_query(rows, calls=None):
    def q(dbname, sql, params=None):
        if calls is not None:
            calls.append({"dbname": dbname, "sql": sql, "params": params})
        return rows
    return q


def _firm_row(**over):
    base = {
        "credit_code": "91361102MACR6J3C5A",
        "name": "江西征吉商贸有限公司",
        "legal_rep": "郑津",
        "reg_capital": 100,
        "paid_capital": None,
        "est_date_raw": 23208,
        "est_year": 2023,
        "status": "存续",
        "company_type": "有限责任公司",
        "registrar": "南昌市市场监督管理局",
        "province": "江西省", "city": "南昌市", "county": None,
        "address": "江西省南昌市……", "business_scope": "许可项目：水产养殖……",
        "former_names": None, "tags": None,
        "batch_year": "2023",
        # internal keys a real SQL row never carries but the projection
        # must drop even if one did:
        "经营范围引用": 26767, "注册地址引用": 131, "曾用名引用": 9,
        "数据源": "2023",
    }
    base.update(over)
    return base


def _regcount_row(**over):
    base = {
        "year": 2023, "province": "河南省", "city": "郑州市",
        "county": None, "industry": "制造业", "industry_code": "C",
        "new_firms": 1234, "bad_total": 0,
        "bad_year": False,
    }
    base.update(over)
    return base


class TestCompanySearchParams:
    def test_both_params_missing_rejected_without_query(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tf, "query", _fake_query([], calls))
        for kwargs in ({}, {"name_prefix": "", "credit_code": ""},
                       {"name_prefix": "   "}):
            out = tf.ia35_company_search(**kwargs)
            assert out["status"] == "invalid_params"
            assert out["brand"] == "finddata"
        assert calls == []

    def test_credit_code_takes_precedence_over_name_prefix(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tf, "query",
                            _fake_query([_firm_row()], calls))
        out = tf.ia35_company_search(
            name_prefix="江西", credit_code="91361102MACR6J3C5A")
        assert calls[0]["params"]["code"] == "91361102MACR6J3C5A"
        assert "prefix" not in calls[0]["params"]
        assert "LIKE" not in calls[0]["sql"]
        assert "忽略" in out["note"]

    def test_credit_path_no_sql_limit_newest_picked_in_python(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tf, "query",
                            _fake_query([_firm_row(), _firm_row()], calls))
        out = tf.ia35_company_search(credit_code="91361102MACR6J3C5A")
        sql = calls[0]["sql"]
        assert 'WHERE f."统一社会信用代码" = :code' in sql
        assert 'ORDER BY f."数据源" DESC, f."核准日期" DESC NULLS LAST' in sql
        assert "LIMIT" not in sql
        assert out["count"] == 1 and out["truncated"] is False

    def test_credit_path_newest_batch_wins(self, monkeypatch):
        rows = [_firm_row(batch_year="2023", est_year=2023),
                _firm_row(batch_year="2019", est_year=2019)]
        monkeypatch.setattr(tf, "query", _fake_query(rows))
        out = tf.ia35_company_search(credit_code="91361102MACR6J3C5A")
        assert out["count"] == 1
        assert out["results"][0]["batch_year"] == 2023
        assert out["results"][0]["est_year"] == 2019 - 2019 + 2023

    def test_credit_path_unknown_code_empty_envelope(self, monkeypatch):
        monkeypatch.setattr(tf, "query", _fake_query([]))
        out = tf.ia35_company_search(credit_code="000000000000000000")
        assert out == {
            "results": [], "count": 0, "truncated": False,
            "snapshot_year": "2023",
            "snapshot_caliber": tf._SNAPSHOT_CALIBER,
            "brand": "finddata",
        }

    def test_name_path_pinned_to_snapshot_year(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tf, "query", _fake_query([], calls))
        out = tf.ia35_company_search(name_prefix="江西")
        sql, params = calls[0]["sql"], calls[0]["params"]
        assert calls[0]["dbname"] == "ia35"
        assert 'f."数据源" = :snapshot' in sql
        assert params["snapshot"] == "2023"
        assert 'f."企业名称" LIKE :prefix' in sql
        assert params["prefix"] == "江西%"
        assert 'ORDER BY f."企业名称"' in sql
        assert params["lim"] == 21  # default limit 20 + 1
        assert out["snapshot_year"] == "2023"

    def test_snapshot_year_env_override_cached_once(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tf, "query", _fake_query([], calls))
        monkeypatch.setattr(tf, "_SNAPSHOT_YEAR", "2022")
        out = tf.ia35_company_search(name_prefix="江西")
        assert calls[0]["params"]["snapshot"] == "2022"
        assert out["snapshot_year"] == "2022"

    def test_like_metacharacters_escaped(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tf, "query", _fake_query([], calls))
        tf.ia35_company_search(name_prefix="50%_赣")
        assert calls[0]["params"]["prefix"] == "50\\%\\_赣%"

    def test_name_limit_clamped_to_200(self, monkeypatch):
        calls = []
        rows = [_firm_row(name=f"公司{i}", credit_code=f"c{i}")
                for i in range(201)]
        monkeypatch.setattr(tf, "query", _fake_query(rows, calls))
        out = tf.ia35_company_search(name_prefix="公司", limit=999)
        assert calls[0]["params"]["lim"] == 201
        assert out["count"] == 200 and out["truncated"] is True

    def test_name_dedup_by_credit_code(self, monkeypatch):
        rows = [_firm_row(name="江西甲有限公司"),
                _firm_row(name="江西甲有限公司", legal_rep="别人")]
        monkeypatch.setattr(tf, "query", _fake_query(rows))
        out = tf.ia35_company_search(name_prefix="江西甲")
        assert out["count"] == 1
        assert out["results"][0]["legal_rep"] == "郑津"  # first row wins

    def test_name_blank_code_dedup_by_name_and_registrar(self, monkeypatch):
        rows = [
            _firm_row(name="江西乙有限公司", credit_code="", registrar="甲局"),
            _firm_row(name="江西乙有限公司", credit_code=None, registrar="甲局"),
            _firm_row(name="江西乙有限公司", credit_code="", registrar="乙局"),
        ]
        monkeypatch.setattr(tf, "query", _fake_query(rows))
        out = tf.ia35_company_search(name_prefix="江西乙")
        assert out["count"] == 2  # (name, registrar) distinguishes 乙局 row

    def test_decode_joins_use_live_confirmed_vocabulary(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tf, "query", _fake_query([], calls))
        tf.ia35_company_search(name_prefix="江西")
        tf.ia35_company_search(credit_code="91361102MACR6J3C5A")
        for call in calls:
            sql = call["sql"]
            assert 'scope."列" = \'经营范围\'' in sql
            assert 'addr."列" = \'注册地址\'' in sql
            assert 'former."列" = \'曾用名\'' in sql
            assert 'scope.ref_key = f."经营范围引用"' in sql
            assert 'addr.ref_key = f."注册地址引用"' in sql
            assert 'former.ref_key = f."曾用名引用"' in sql
            # decode is batch-local: the join carries the row's own 数据源
            assert 'scope."数据源" = f."数据源"' in sql

    def test_decode_miss_surfaces_null_not_error(self, monkeypatch):
        monkeypatch.setattr(tf, "query", _fake_query(
            [_firm_row(address=None, business_scope=None,
                       former_names=None)]))
        out = tf.ia35_company_search(credit_code="91361102MACR6J3C5A")
        r = out["results"][0]
        assert r["address"] is None and r["business_scope"] is None
        assert r["former_names"] is None

    def test_est_date_normalized_from_raw(self, monkeypatch):
        monkeypatch.setattr(tf, "query",
                            _fake_query([_firm_row(est_date_raw=23208)]))
        assert tf.ia35_company_search(
            credit_code="x")["results"][0]["est_date"] == "20230717"

    def test_raw_refs_and_batch_keys_never_leak(self, monkeypatch):
        monkeypatch.setattr(tf, "query", _fake_query(
            [_firm_row(), _firm_row(credit_code="c2", name="另一家")]))
        for path_kwargs in ({"credit_code": "x"}, {"name_prefix": "江西"}):
            out = tf.ia35_company_search(**path_kwargs)
            for r in out["results"]:
                assert not (_FORBIDDEN & set(r))
                assert set(r) == _RESULT_KEYS

    def test_brand_on_rows(self, monkeypatch):
        monkeypatch.setattr(tf, "query", _fake_query([_firm_row()]))
        out = tf.ia35_company_search(name_prefix="江西")
        assert all(r["brand"] == "finddata" for r in out["results"])


class TestEstDateNormalization:
    def test_yyyymmdd_shaped_int_served_verbatim(self):
        assert tf._norm_est_date(20230115) == "20230115"

    def test_invalid_calendar_yyyymmdd_falls_to_serial_decode(self):
        # 20230230 is not a calendar date; as a 1960-epoch serial it lands
        # far outside 1900-2100 -> null.
        assert tf._norm_est_date(20230230) is None

    def test_1960_epoch_serial_decodes(self):
        # live-verified encoding: 成立日期 23208 ↔ 成立年份 2023 (2023 batch)
        assert tf._norm_est_date(23208) == "20230717"
        assert tf._norm_est_date(16126) == "20040225"

    def test_out_of_range_serial_is_null(self):
        assert tf._norm_est_date(51135) == "21000101"   # window edge, inclusive
        assert tf._norm_est_date(51501) is None          # 2101-01-01
        assert tf._norm_est_date(99999999) is None
        assert tf._norm_est_date(-99999) is None

    def test_none_and_non_numeric_are_null(self):
        assert tf._norm_est_date(None) is None
        assert tf._norm_est_date(True) is None
        assert tf._norm_est_date(" nonsense ") is None

    def test_batch_year_becomes_int(self):
        assert tf._batch_year("2023") == 2023
        assert tf._batch_year(" nonyear ") is None
        assert tf._batch_year(None) is None


class TestRegCountsRead:
    def test_serial_conversion_expression_in_sql(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tf, "query", _fake_query([], calls))
        tf.ia35_reg_counts_read()
        sql = calls[0]["sql"]
        assert calls[0]["dbname"] == "ia35"
        # Excel-serial branch on the 1899-12-30 epoch (anchor 32741 → 1989)
        assert "EXTRACT(year FROM DATE '1899-12-30' + \"成立年份\")" in sql
        assert '"成立年份" <= 2027' in sql and '"成立年份" > 2027' in sql
        assert "BETWEEN 1900 AND 2100" in sql

    def test_year_bounds_filter_normalized_year(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tf, "query", _fake_query([], calls))
        tf.ia35_reg_counts_read(start_year=2015, end_year=2020)
        sql, params = calls[0]["sql"], calls[0]["params"]
        assert params["y1"] == 2015 and params["y2"] == 2020
        # raw-column branch keeps the (省份, 成立年份) index usable…
        assert '("成立年份" <= 2027 AND "成立年份" >= :y1 AND "成立年份" <= :y2)' in sql
        assert 'OR ("成立年份" > 2027)' in sql
        # …while the converted-year interval rides as a post-filter.
        assert "WHERE w.year IS NOT NULL AND w.year >= :y1 AND w.year <= :y2" in sql
        # anomaly disclosure is computed below the outer WHERE, in one scan
        assert "count(*) FILTER (WHERE s.bad_year) OVER () AS bad_total" in sql

    def test_province_and_industry_filters_hit_sql(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tf, "query", _fake_query([], calls))
        tf.ia35_reg_counts_read(province=" 河南省 ", industry="A")
        sql, params = calls[0]["sql"], calls[0]["params"]
        assert params["province"] == "河南省"
        assert '"省份" = :province' in sql
        assert params["industry"] == "A"
        assert '("行业门类代码" = :industry OR "行业门类" = :industry)' in sql

    def test_true_year_passes_through_untouched(self, monkeypatch):
        monkeypatch.setattr(tf, "query",
                            _fake_query([_regcount_row(year=2023)]))
        out = tf.ia35_reg_counts_read()
        assert out["results"][0]["year"] == 2023

    def test_bad_year_rows_counted_into_note(self, monkeypatch):
        monkeypatch.setattr(tf, "query",
                            _fake_query([_regcount_row(bad_total=32741)]))
        out = tf.ia35_reg_counts_read()
        assert out["note"] == "32741 行年份异常被排除"

    def test_no_note_when_nothing_dropped(self, monkeypatch):
        monkeypatch.setattr(tf, "query",
                            _fake_query([_regcount_row(bad_total=0)]))
        out = tf.ia35_reg_counts_read()
        assert "note" not in out

    def test_missing_buckets_not_zero_filled(self, monkeypatch):
        rows = [_regcount_row(year=2020, province="河南省", city="郑州市"),
                _regcount_row(year=2022, province="河南省", city="郑州市")]
        monkeypatch.setattr(tf, "query", _fake_query(rows))
        out = tf.ia35_reg_counts_read(start_year=2020, end_year=2022)
        # 2021 is simply absent — no filler row, no zero
        assert [r["year"] for r in out["results"]] == [2020, 2022]
        assert out["results"][0]["county"] is None  # null stays null, not 0

    def test_limit_clamped_1_to_5000(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tf, "query", _fake_query([], calls))
        tf.ia35_reg_counts_read(limit=999999)
        assert calls[0]["params"]["lim"] == 5001
        tf.ia35_reg_counts_read(limit=0)
        assert calls[1]["params"]["lim"] == 2

    def test_order_by_year_then_province(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tf, "query", _fake_query([], calls))
        tf.ia35_reg_counts_read()
        assert "ORDER BY year, province" in calls[0]["sql"]

    def test_row_keys_exactly_public_fields(self, monkeypatch):
        monkeypatch.setattr(tf, "query",
                            _fake_query([_regcount_row(bad_total=5)]))
        out = tf.ia35_reg_counts_read()
        assert set(out["results"][0]) == _REGCOUNT_KEYS
        assert not (_FORBIDDEN & set(out["results"][0]))

    def test_truncation_flagged(self, monkeypatch):
        rows = [_regcount_row(year=y, province=f"省{y}") for y in range(5)]
        monkeypatch.setattr(tf, "query", _fake_query(rows))
        out = tf.ia35_reg_counts_read(limit=3)
        assert out["count"] == 3 and out["truncated"] is True

    def test_domain_unavailable_passthrough(self, monkeypatch):
        fail = {"status": "domain_unavailable", "detail": "OperationalError"}
        monkeypatch.setattr(tf, "query", _fake_query(fail))
        assert tf.ia35_reg_counts_read() == fail
        monkeypatch.setattr(tf, "query", _fake_query(fail))
        assert tf.ia35_company_search(credit_code="x") == fail
        monkeypatch.setattr(tf, "query", _fake_query(fail))
        assert tf.ia35_company_search(name_prefix="江西") == fail


class TestRegCountsLive:
    """Live tail against the real ia35 reg_counts (skipped without env).

    reg_counts plans cleanly today (bitmap over idx_reg_counts_prov_year),
    so province+year probes actually run; a transient
    domain_unavailable skips (environmental), never fails.
    """

    @staticmethod
    def _read_or_skip(*args, **kwargs):
        out = tf.ia35_reg_counts_read(*args, **kwargs)
        if out.get("status") == "domain_unavailable":
            pytest.skip("federated ia35 timed out (remote load)")
        return out

    @needs_pg
    def test_real_true_years_serve_untouched(self):
        out = self._read_or_skip(start_year=2023, end_year=2023,
                                 province="河南省", limit=50)
        assert out["brand"] == "finddata"
        assert out["count"] > 0
        assert all(r["year"] == 2023 for r in out["results"])

    @needs_pg
    def test_real_serial_years_convert_into_range(self):
        # 成立年份 32741 (Excel serial) → 1989: converted rows must surface
        # as year 1989 and stay inside 1900-2100 everywhere.
        out = self._read_or_skip(start_year=1989, end_year=1989,
                                 province="河南省", limit=50)
        assert all(r["year"] == 1989 for r in out["results"])
        wide = self._read_or_skip(province="河南省", limit=5000)
        assert all(1900 <= r["year"] <= 2100 for r in wide["results"])

    @needs_pg
    def test_real_rows_carry_no_internal_keys(self):
        out = self._read_or_skip(start_year=2020, end_year=2024,
                                 province="河南省", limit=10)
        for r in out["results"]:
            assert set(r) == _REGCOUNT_KEYS
            assert not (_FORBIDDEN & set(r))


class TestCompanySearchDecode:
    """2026-10-12: firms_full has NO 企业类型/经营状态/登记机关 text columns.

    The pre-fix tool selected those names and could never serve a row. The
    enum-ish columns are ``*引用`` integers decoded through firm_dict —
    these tests pin that decode so the defect cannot silently return.
    """

    def test_enum_columns_decode_through_firm_dict(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tf, "query", _fake_query([], calls))
        tf.ia35_company_search(name_prefix="泰兴")
        sql = calls[0]["sql"]
        # The three broken text-column references must be gone.
        for dead in ('f."企业类型"', 'f."经营状态"', 'f."登记机关"'):
            assert dead not in sql
        # ...and replaced by firm_dict joins on the *引用 int columns.
        for alias, col, ref in tf._DICT_JOINS:
            assert f'LEFT JOIN firm_dict {alias}' in sql
            assert f'{alias}."列" = \'{col}\'' in sql
            assert f'{alias}.id = f."{ref}"' in sql
        # Long-text decodes stay on the batch-scoped strl_texts registry.
        for alias, col, ref in tf._DECODE_JOINS:
            assert f'LEFT JOIN strl_texts {alias}' in sql

    def test_no_batch_predicate_on_firm_dict_joins(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tf, "query", _fake_query([], calls))
        tf.ia35_company_search(credit_code="91361102MACR6J3C5A")
        sql = calls[0]["sql"]
        # firm_dict is global — no 数据源 predicate, unlike strl_texts.
        for alias in ("ctype", "cstatus", "cregistrar"):
            line = next(l for l in sql.splitlines()
                        if f"LEFT JOIN firm_dict {alias}" in l)
            assert "数据源" not in line

    def test_company_type_filter_predicates_decoded_text(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tf, "query", _fake_query([], calls))
        out = tf.ia35_company_search(name_prefix="泰兴", company_type="个体工商户")
        sql, params = calls[0]["sql"], calls[0]["params"]
        # Filter on the decoded text, never the raw integer ref.
        assert 'ctype.txt = :ctype' in sql
        assert params["ctype"] == "个体工商户"
        assert out["company_type"] == "个体工商户"

    def test_company_type_filter_applies_to_credit_path(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tf, "query", _fake_query([], calls))
        out = tf.ia35_company_search(credit_code="X", company_type="个体工商户")
        assert 'ctype.txt = :ctype' in calls[0]["sql"]
        assert out["company_type"] == "个体工商户"

    def test_company_type_not_echoed_when_absent(self, monkeypatch):
        monkeypatch.setattr(tf, "query", _fake_query([]))
        out = tf.ia35_company_search(name_prefix="泰兴")
        assert "company_type" not in out

    def test_decoded_status_and_registrar_reach_the_payload(self, monkeypatch):
        row = _firm_row()
        row.pop("company_type", None)
        row.pop("status", None)
        row.pop("registrar", None)
        # Simulate what the fixed SELECT returns: decoded dictionary text.
        row["company_type"] = "个体工商户"
        row["status"] = "存续"
        row["registrar"] = "泰州市市场监督管理局"
        monkeypatch.setattr(tf, "query", _fake_query([row]))
        out = tf.ia35_company_search(name_prefix="泰兴")
        hit = out["results"][0]
        assert hit["company_type"] == "个体工商户"
        assert hit["status"] == "存续"
        assert hit["registrar"] == "泰州市市场监督管理局"

    def test_missing_dictionary_entry_surfaces_as_null(self, monkeypatch):
        row = _firm_row()
        row["company_type"] = None
        monkeypatch.setattr(tf, "query", _fake_query([row]))
        out = tf.ia35_company_search(name_prefix="泰兴")
        # A LEFT JOIN miss is null — never a raw ref int, never a guess.
        assert out["results"][0]["company_type"] is None


class TestSnapshotCaliberDisclosure:
    """The corpus is a yearly batch snapshot, not live registry state."""

    def test_search_responses_carry_caliber(self, monkeypatch):
        monkeypatch.setattr(tf, "query", _fake_query([]))
        out = tf.ia35_company_search(name_prefix="泰兴")
        assert out["snapshot_year"] == "2023"
        assert "非实时" in out["snapshot_caliber"]

    def test_regcounts_responses_carry_latest_year(self, monkeypatch):
        monkeypatch.setattr(tf, "query", _fake_query(
            [{"year": 2021, "bad_total": 0}, {"year": 2023, "bad_total": 0}]))
        out = tf.ia35_reg_counts_read(province="河南省")
        assert out["latest_year_available"] == 2023
        assert "非实时" in out["snapshot_caliber"]

    def test_regcounts_latest_year_null_on_empty(self, monkeypatch):
        monkeypatch.setattr(tf, "query", _fake_query([]))
        out = tf.ia35_reg_counts_read(province="河南省")
        assert out["latest_year_available"] is None


class TestRegCountsGranularity:
    """2026-10-12: expose city/county and the four industry levels."""

    def test_city_filter_hits_sql(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tf, "query", _fake_query([], calls))
        tf.ia35_reg_counts_read(province="广东省", city="广州市")
        sql, params = calls[0]["sql"], calls[0]["params"]
        assert '"城市" = :city' in sql
        assert params["city"] == "广州市"

    def test_county_filter_hits_sql(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tf, "query", _fake_query([], calls))
        tf.ia35_reg_counts_read(province="广东省", county="天河区")
        sql, params = calls[0]["sql"], calls[0]["params"]
        assert '"区县" = :county' in sql
        assert params["county"] == "天河区"

    def test_industry_class_defaults_to_section_level(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tf, "query", _fake_query([], calls))
        tf.ia35_reg_counts_read(province="广东省", industry_class="H")
        sql = calls[0]["sql"]
        assert '"行业门类代码" = :iclass OR "行业门类" = :iclass' in sql

    def test_industry_class_at_small_class_level(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tf, "query", _fake_query([], calls))
        tf.ia35_reg_counts_read(province="广东省", industry_class="快餐服务",
                                industry_level="小类")
        sql, params = calls[0]["sql"], calls[0]["params"]
        assert '"行业小类代码" = :iclass OR "行业小类" = :iclass' in sql
        assert params["iclass"] == "快餐服务"

    def test_unknown_industry_level_rejected(self, monkeypatch):
        monkeypatch.setattr(tf, "query", _fake_query([]))
        out = tf.ia35_reg_counts_read(province="广东省", industry_class="H",
                                      industry_level="细类")
        assert out["status"] == "invalid_params"
        assert "industry_level" in out["detail"]

    def test_city_without_province_rejected(self, monkeypatch):
        """Only 省份 is indexed; a bare city filter would scan 12.4M rows."""
        monkeypatch.setattr(tf, "query", _fake_query([]))
        out = tf.ia35_reg_counts_read(city="广州市")
        assert out["status"] == "invalid_params"
        assert "province" in out["detail"]

    def test_county_without_province_rejected(self, monkeypatch):
        monkeypatch.setattr(tf, "query", _fake_query([]))
        out = tf.ia35_reg_counts_read(county="天河区")
        assert out["status"] == "invalid_params"

    def test_legacy_call_shape_unchanged(self, monkeypatch):
        """Province+industry-only callers must see the same SQL shape."""
        calls = []
        monkeypatch.setattr(tf, "query", _fake_query([], calls))
        tf.ia35_reg_counts_read(province=" 河南省 ", industry="A")
        sql, params = calls[0]["sql"], calls[0]["params"]
        assert params["province"] == "河南省"
        assert '"省份" = :province' in sql
        assert '("行业门类代码" = :industry OR "行业门类" = :industry)' in sql
        # No new predicates leak in when the new params are absent.
        assert '"城市" = :city' not in sql
        assert '"区县" = :county' not in sql
        assert ":iclass" not in sql
