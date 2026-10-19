"""Tests for the wenshu domain tools (tools_wenshu.py).

Unit tests fake the domain query and the body fetch — no network, no
RustFS. A small live tail (skipped without FDBIZ_DOMAIN_PG_URL) exercises
one real decoded search against wenshu_db.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import fd_find_data_business_mcp.tools_wenshu as tw
import fd_find_data_business_mcp.wenshu_body as wb

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
    "id_origin", "block_key", "block_ordinal", "src_id",
    "法院引用", "案由引用", "省引用", "市引用", "案件类型引用",
    "文书类型引用", "审理程序引用", "案例级别引用", "法院级别引用", "法条引用",
}

_SEARCH_ROW_KEYS = {
    "doc_id", "案号", "标题", "法院", "案由", "省", "市", "案件类型",
    "文书类型", "审理程序", "法院级别", "判决年份", "判决结果", "发布时间",
    "has_body", "brand",
}


def _fake_query(rows, calls=None):
    def q(dbname, sql, params=None):
        if calls is not None:
            calls.append({"dbname": dbname, "sql": sql, "params": params})
        return rows
    return q


def _search_row(title="借款合同纠纷一审", **over):
    base = {
        "doc_id": "f6594c3d5ac779a9", "案号": "（2024）京01民初1号",
        "标题": title, "法院": "北京市第一中级人民法院",
        "案由": "借款合同纠纷", "省": "北京市", "市": "北京市",
        "案件类型": "民事一审", "文书类型": "判决书",
        "审理程序": "一审普通程序", "法院级别": "中级法院",
        "判决年份": 2024, "判决结果": "部分支持", "发布时间": "2024-06-01",
        "has_body": True, "rank": 2,
        # internal coordinates / raw refs a real SQL row never carries but
        # the projection must drop even if one did:
        "id_origin": "origin-1", "block_key": "blocks/2024/09/000711.zst",
        "block_ordinal": 7, "src_id": 3,
        "法院引用": 12, "案由引用": 34, "省引用": 1, "市引用": 2,
        "案件类型引用": 3, "文书类型引用": 4, "审理程序引用": 5,
        "案例级别引用": 1, "法院级别引用": 2, "法条引用": 99,
    }
    base.update(over)
    return base


def _read_row(has_block=True, body_shape="html", **over):
    row = _search_row(**over)
    row.pop("rank")
    row["判决时间"] = "2024-05-20"
    row["受理时间"] = "2024-02-01"
    row["body_shape"] = body_shape
    if not has_block:
        row["block_key"] = None
    return row


def _patch_fetch(monkeypatch, result):
    calls = []

    def fetch(block_key, doc_id):
        calls.append({"block_key": block_key, "doc_id": doc_id})
        return result

    monkeypatch.setattr(wb, "fetch_judgment_body", fetch)
    return calls


class TestWenshuSearchUnit:
    def test_ranking_exact_prefix_contains(self, monkeypatch):
        rows = [
            _search_row("关于借款合同纠纷的规定", doc_id="d4"),
            _search_row("借款合同纠纷释义", doc_id="d1"),
            _search_row("借款合同纠纷", doc_id="d2"),
            _search_row("借款合同纠纷二审", doc_id="d3"),
        ]
        monkeypatch.setattr(tw, "query", _fake_query(rows))
        out = tw.wenshu_search("借款合同纠纷")
        assert [r["doc_id"] for r in out["results"]] == ["d2", "d3", "d1", "d4"]

    def test_row_keys_exactly_public_metadata(self, monkeypatch):
        monkeypatch.setattr(tw, "query", _fake_query([_search_row()]))
        out = tw.wenshu_search("借款")
        assert set(out["results"][0]) == _SEARCH_ROW_KEYS

    def test_internal_coordinates_never_leak(self, monkeypatch):
        """block_key/src_id/refs planted in the row must not survive."""
        monkeypatch.setattr(tw, "query", _fake_query(
            [_search_row(), _search_row("裁定书某篇", doc_id="d2")]))
        out = tw.wenshu_search("纠纷")
        for r in out["results"]:
            assert not (_FORBIDDEN & set(r))
            assert "block_key" not in r and "src_id" not in r
            assert "法条引用" not in r and "id_origin" not in r

    def test_search_sql_never_selects_coordinates(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tw, "query", _fake_query([], calls))
        tw.wenshu_search("条例")
        sql = calls[0]["sql"]
        for col in ("id_origin", "block_ordinal", "src_id", "案例级别",
                    "法条引用"):
            assert col not in sql
        # block_key appears only inside the has_body guard, never projected.
        assert sql.count('"block_key"') == 1
        assert '"block_key" IS NOT NULL' in sql
        assert "AS block_key" not in sql

    def test_has_body_reflected_as_bool(self, monkeypatch):
        rows = [
            _search_row("有正文篇", doc_id="d1", has_body=True),
            _search_row("无正文篇", doc_id="d2", has_body=False),
        ]
        monkeypatch.setattr(tw, "query", _fake_query(rows))
        out = tw.wenshu_search("篇")
        flags = {r["doc_id"]: r["has_body"] for r in out["results"]}
        assert flags == {"d1": True, "d2": False}

    def test_results_bounded_and_truncated(self, monkeypatch):
        rows = [_search_row(f"文书{i}", doc_id=f"d{i}") for i in range(5)]
        monkeypatch.setattr(tw, "query", _fake_query(rows))
        out = tw.wenshu_search("文书", limit=3)
        assert out["count"] == 3 and len(out["results"]) == 3
        assert out["truncated"] is True

    def test_not_truncated_when_within_limit(self, monkeypatch):
        rows = [_search_row(f"文书{i}", doc_id=f"d{i}") for i in range(2)]
        monkeypatch.setattr(tw, "query", _fake_query(rows))
        out = tw.wenshu_search("文书", limit=2)
        assert out["count"] == 2 and out["truncated"] is False

    def test_limit_capped_at_200(self, monkeypatch):
        calls = []
        rows = [_search_row(f"文书{i}", doc_id=f"d{i}") for i in range(201)]
        monkeypatch.setattr(tw, "query", _fake_query(rows, calls))
        out = tw.wenshu_search("文书", limit=500)
        assert calls[0]["params"]["lim"] == 201
        assert out["count"] == 200 and out["truncated"] is True

    def test_empty_or_whitespace_query_returns_empty_envelope(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tw, "query", _fake_query([], calls))
        for q in ("", "   "):
            assert tw.wenshu_search(q) == {
                "results": [], "count": 0, "truncated": False, "brand": "finddata"}
        assert calls == []

    def test_doc_type_filter_hits_sql(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tw, "query", _fake_query([], calls))
        tw.wenshu_search("合同", doc_type="判决书")
        assert calls[0]["params"]["doc_type"] == "判决书"
        assert "AND dwt.txt = :doc_type" in calls[0]["sql"]
        calls.clear()
        tw.wenshu_search("合同")
        assert "doc_type" not in calls[0]["params"]
        assert "AND dwt.txt = :doc_type" not in calls[0]["sql"]

    def test_province_filter_hits_sql(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tw, "query", _fake_query([], calls))
        tw.wenshu_search("合同", province="  河南省  ")
        assert calls[0]["params"]["province"] == "河南省"
        assert "AND dp.txt = :province" in calls[0]["sql"]

    def test_year_interval_closed(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tw, "query", _fake_query([], calls))
        tw.wenshu_search("合同", start_year=2015, end_year=2020)
        assert calls[0]["params"]["start_year"] == 2015
        assert calls[0]["params"]["end_year"] == 2020
        sql = calls[0]["sql"]
        assert 'AND j."判决年份" >= :start_year' in sql
        assert 'AND j."判决年份" <= :end_year' in sql
        calls.clear()
        tw.wenshu_search("合同")
        assert "start_year" not in calls[0]["params"]
        assert "end_year" not in calls[0]["params"]

    def test_like_metacharacters_escaped(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tw, "query", _fake_query([], calls))
        tw.wenshu_search("50%_of")
        p = calls[0]["params"]
        assert p["exact"] == "50\\%\\_of"
        assert p["prefix"] == "50\\%\\_of%"
        assert p["pattern"] == "%50\\%\\_of%"

    def test_decode_joins_and_time_cast_in_sql(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tw, "query", _fake_query([], calls))
        tw.wenshu_search("合同")
        sql = calls[0]["sql"]
        for ref in ("法院引用", "案由引用", "省引用", "市引用", "案件类型引用",
                    "文书类型引用", "审理程序引用", "法院级别引用"):
            assert f"ref_key = j.\"{ref}\"" in sql
        assert 'j."发布时间"::date::text AS 发布时间' in sql
        assert calls[0]["dbname"] == "wenshu_db"
        assert calls[0]["params"]["lim"] == 51  # default limit 50 + 1

    def test_brand_on_envelope_and_rows(self, monkeypatch):
        monkeypatch.setattr(tw, "query", _fake_query([_search_row()]))
        out = tw.wenshu_search("纠纷")
        assert out["brand"] == "finddata"
        assert all(r["brand"] == "finddata" for r in out["results"])

    def test_domain_unavailable_passthrough(self, monkeypatch):
        fail = {"status": "domain_unavailable", "detail": "OperationalError"}
        monkeypatch.setattr(tw, "query", _fake_query(fail))
        assert tw.wenshu_search("合同") == fail


class TestWenshuReadUnit:
    def test_read_with_body_full_payload(self, monkeypatch):
        fetch_calls = _patch_fetch(monkeypatch, "本院认为……判决如下：")
        monkeypatch.setattr(tw, "query", _fake_query([_read_row()]))
        out = tw.wenshu_read("f6594c3d5ac779a9")
        assert out["doc_id"] == "f6594c3d5ac779a9"
        assert out["content_available"] is True
        assert out["content"] == "本院认为……判决如下："
        assert out["body_shape"] == "html"
        assert out["判决时间"] == "2024-05-20"
        assert out["受理时间"] == "2024-02-01"
        assert out["发布时间"] == "2024-06-01"
        assert out["法院"] == "北京市第一中级人民法院"
        assert out["brand"] == "finddata"
        assert "note" not in out
        assert not (_FORBIDDEN & set(out))
        assert fetch_calls == [{"block_key": "blocks/2024/09/000711.zst",
                                "doc_id": "f6594c3d5ac779a9"}]

    def test_read_row_keys_exactly(self, monkeypatch):
        _patch_fetch(monkeypatch, "正文")
        monkeypatch.setattr(tw, "query", _fake_query([_read_row()]))
        out = tw.wenshu_read("f6594c3d5ac779a9")
        expected = ((_SEARCH_ROW_KEYS - {"has_body"})
                    | {"判决时间", "受理时间", "body_shape",
                       "content_available", "content",
                       # 体积上限契约（mcp-result-contract）：正文按窗口返回，
                       # 这三个字段始终给出，调用方据此续读。
                       "total_chars", "content_offset", "content_truncated"})
        assert set(out) == expected

    def test_read_none_shape_no_content_key_no_fetch(self, monkeypatch):
        fetch_calls = _patch_fetch(monkeypatch, "SHOULD-NOT-BE-FETCHED")
        monkeypatch.setattr(tw, "query",
                            _fake_query([_read_row(has_block=False,
                                                   body_shape="none")]))
        out = tw.wenshu_read("f6594c3d5ac779a9")
        assert out["content_available"] is False
        assert "content" not in out  # absent, not an empty string
        assert "仅元数据入库" in out["note"]
        assert fetch_calls == []

    def test_read_null_block_key_guarded_even_with_html_shape(self, monkeypatch):
        _patch_fetch(monkeypatch, "SHOULD-NOT-BE-FETCHED")
        monkeypatch.setattr(tw, "query",
                            _fake_query([_read_row(has_block=True,
                                                   body_shape="html",
                                                   block_key=None)]))
        out = tw.wenshu_read("f6594c3d5ac779a9")
        assert out["content_available"] is False
        assert "content" not in out
        assert "仅元数据入库" in out["note"]

    def test_read_fetch_failure_degrades_with_retry_note(self, monkeypatch):
        _patch_fetch(monkeypatch, None)  # RustFS unreachable / corrupt block
        monkeypatch.setattr(tw, "query", _fake_query([_read_row()]))
        out = tw.wenshu_read("f6594c3d5ac779a9")
        assert out["content_available"] is False
        assert "content" not in out
        assert "暂时不可读" in out["note"]
        assert out["标题"] == "借款合同纠纷一审"  # metadata still complete
        assert not (_FORBIDDEN & set(out))

    def test_read_not_found(self, monkeypatch):
        monkeypatch.setattr(tw, "query", _fake_query([]))
        assert tw.wenshu_read("nope") == {"status": "not_found",
                                          "brand": "finddata"}

    def test_read_blank_doc_id_skips_query(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tw, "query", _fake_query([], calls))
        for blank in ("", "   "):
            assert tw.wenshu_read(blank) == {"status": "not_found",
                                             "brand": "finddata"}
        assert calls == []

    def test_read_id_bound_as_param(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tw, "query", _fake_query([], calls))
        tw.wenshu_read("f6594c3d5ac779a9")
        assert calls[0]["dbname"] == "wenshu_db"
        assert calls[0]["params"] == {"doc_id": "f6594c3d5ac779a9"}
        assert 'WHERE j."doc_id" = :doc_id' in calls[0]["sql"]
        # read needs the block coordinate internally…
        assert '"block_key"' in calls[0]["sql"]
        # …but projects body_shape, not the coordinate itself.
        assert "AS body_shape" in calls[0]["sql"]
        assert 'j."判决时间"::date::text AS 判决时间' in calls[0]["sql"]
        assert 'j."受理时间"::date::text AS 受理时间' in calls[0]["sql"]

    def test_read_tolerates_null_optional_fields(self, monkeypatch):
        _patch_fetch(monkeypatch, None)
        row = _read_row(body_shape="none")
        for k in ("案号", "法院", "案由", "省", "市", "判决结果",
                  "发布时间", "判决时间", "受理时间", "body_shape"):
            row[k] = None
        monkeypatch.setattr(tw, "query", _fake_query([row]))
        out = tw.wenshu_read("f6594c3d5ac779a9")
        assert out["content_available"] is False
        assert out["案由"] is None

    def test_domain_unavailable_passthrough(self, monkeypatch):
        fail = {"status": "domain_unavailable", "detail": "TimeoutError"}
        monkeypatch.setattr(tw, "query", _fake_query(fail))
        assert tw.wenshu_read("f6594c3d5ac779a9") == fail


class TestSearchReadConsistency:
    def test_has_body_flag_matches_read_availability(self, monkeypatch):
        monkeypatch.setattr(tw, "query", _fake_query(
            [_search_row("有正文篇", doc_id="d1", has_body=True)]))
        assert tw.wenshu_search("有正文篇")["results"][0]["has_body"] is True
        _patch_fetch(monkeypatch, "正文")
        monkeypatch.setattr(tw, "query",
                            _fake_query([_read_row(doc_id="d1")]))
        assert tw.wenshu_read("d1")["content_available"] is True

        monkeypatch.setattr(tw, "query", _fake_query(
            [_search_row("无正文篇", doc_id="d2", has_body=False)]))
        assert tw.wenshu_search("无正文篇")["results"][0]["has_body"] is False
        _patch_fetch(monkeypatch, "SHOULD-NOT-BE-FETCHED")
        monkeypatch.setattr(tw, "query", _fake_query(
            [_read_row(doc_id="d2", has_block=False, body_shape="none")]))
        got = tw.wenshu_read("d2")
        assert got["content_available"] is False and "content" not in got


class TestWenshuSearchLive:
    """Live tail against the real read-only wenshu_db (skipped without env).

    Probes are deliberately selective (maritime collisions — a few dozen
    snapshot hits): broad one-word probes scan hundreds of thousands of
    trgm matches before the rank sort and legitimately exceed the 15s
    statement timeout, surfacing as domain_unavailable. That envelope is
    documented in tools_wenshu's docstring, not papered over here. Even
    selective probes can catch the shared data machine under load, so a
    transient domain_unavailable skips (environmental), never fails.
    """

    @staticmethod
    def _search_or_skip(*args, **kwargs):
        out = tw.wenshu_search(*args, **kwargs)
        if out.get("status") == "domain_unavailable":
            pytest.skip("federated wenshu_db timed out (remote load)")
        return out

    @needs_pg
    def test_real_search_returns_decoded_metadata(self):
        out = self._search_or_skip("船舶碰撞", limit=5)
        assert out["brand"] == "finddata"
        if out["count"] == 0:  # corpus window gaps are a documented reality
            pytest.skip("snapshot has no hits for the probe term")
        for hit in out["results"]:
            assert set(hit) == _SEARCH_ROW_KEYS
            assert not (_FORBIDDEN & set(hit))
            assert hit["判决年份"] is None or isinstance(hit["判决年份"], int)

    @needs_pg
    def test_real_year_interval_narrows_and_decodes(self):
        wide = self._search_or_skip("船舶碰撞", start_year=2015,
                                    end_year=2024, limit=200)
        if not wide["count"]:
            pytest.skip("snapshot has no hits for the probe term")
        assert all(2015 <= (r["判决年份"] or 0) <= 2024
                   for r in wide["results"])
        narrow = self._search_or_skip("船舶碰撞", start_year=2020,
                                      end_year=2021, limit=200)
        assert narrow["count"] <= wide["count"]
        assert all(r["判决年份"] in (2020, 2021) for r in narrow["results"])
        # decode JOINs actually resolve on live data (100% hit rate verified
        # 2026-09-28): some hit carries a real court name.
        assert any(r["法院"] for r in wide["results"])
