"""Tests for the guide-cases tools (tools_guide_cases.py).

Unit tests fake the domain query — no network. Integration tests hit the
real federated PG via FDBIZ_DOMAIN_PG_URL, loaded from the repo .env when
absent from the environment.
"""
from __future__ import annotations

import os
import pathlib

import pytest

import fd_find_data_business_mcp.tools_guide_cases as tools_gc

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

if not os.environ.get("FDBIZ_DOMAIN_PG_URL"):
    _env = _REPO_ROOT / ".env"
    if _env.exists():
        for _line in _env.read_text().splitlines():
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _val = _line.partition("=")
                os.environ.setdefault(_key.strip(), _val.strip())

_FORBIDDEN = {"content_hash", "fetched_at"}

_SECTIONS = ("keyword", "gist", "facts", "result", "reasoning", "statutes")


def _fake_query(rows, calls=None):
    def q(dbname, sql, params=None):
        if calls is not None:
            calls.append({"dbname": dbname, "sql": sql, "params": params})
        return rows
    return q


def _row(title="某指导性案例", **over):
    base = {
        "source_id": "490521", "case_no": 1, "doc_type": "case",
        "title": title,
        "listed_at": "2020-01-14 00:00:00+08",
        "publish_at": "2020-01-14 00:00:00+08",
        "source": "最高人民法院", "source_url": "https://example.court/490521",
        "keyword": "民间借贷", "gist": "裁判要点", "facts": "基本案情",
        "result": "裁判结果", "reasoning": "裁判理由", "statutes": "相关法条",
        "body": "FULL-BODY",
        "reference_status": "有效", "reference_note": None,
        # loaded_at：本表无 created_at，provenance.as_of 取入库时间。
        "loaded_at": "2026-09-08 00:00:00",
        # poisoned provenance — must never surface in any response
        "content_hash": "SECRET-HASH", "fetched_at": "2025-01-01T00:00:00+08",
    }
    base.update(over)
    return base


class TestGuideCasesSearchUnit:
    def test_ranking_exact_prefix_contains(self, monkeypatch):
        rows = [
            _row("关于借贷的指导性案例说明", source_id="9004"),
            _row("最高人民法院指导性案例解读", source_id="9001"),
            _row("指导性案例", source_id="9002"),
            _row("指导性案例实施指引", source_id="9003"),
        ]
        monkeypatch.setattr(tools_gc, "query", _fake_query(rows))
        out = tools_gc.guide_cases_search("指导性案例")
        assert [r["source_id"] for r in out["results"]] == [
            "9002", "9003", "9004", "9001"]

    def test_results_carry_reference_status(self, monkeypatch):
        rows = [
            _row("指导性案例一", reference_status="有效"),
            _row("指导性案例二", reference_status="不再参照"),
        ]
        monkeypatch.setattr(tools_gc, "query", _fake_query(rows))
        out = tools_gc.guide_cases_search("指导性案例")
        assert [(r["title"], r["reference_status"]) for r in out["results"]] == [
            ("指导性案例一", "有效"), ("指导性案例二", "不再参照")]

    def test_truncated_when_more_matches_than_limit(self, monkeypatch):
        rows = [_row(f"指导性案例{i}", source_id=str(i)) for i in range(3)]
        monkeypatch.setattr(tools_gc, "query", _fake_query(rows))
        out = tools_gc.guide_cases_search("指导性案例", limit=2)
        assert out["count"] == 2
        assert len(out["results"]) == 2
        assert out["truncated"] is True

    def test_not_truncated_when_within_limit(self, monkeypatch):
        rows = [_row(f"指导性案例{i}", source_id=str(i)) for i in range(2)]
        monkeypatch.setattr(tools_gc, "query", _fake_query(rows))
        out = tools_gc.guide_cases_search("指导性案例", limit=2)
        assert out["count"] == 2
        assert out["truncated"] is False

    def test_limit_capped_at_200(self, monkeypatch):
        calls = []
        rows = [_row(f"案例{i}", source_id=str(i)) for i in range(201)]
        monkeypatch.setattr(tools_gc, "query", _fake_query(rows, calls))
        out = tools_gc.guide_cases_search("案例", limit=500)
        assert calls[0]["params"]["lim"] == 201
        assert out["count"] == 200
        assert len(out["results"]) == 200
        assert out["truncated"] is True

    def test_empty_or_whitespace_query_returns_empty_envelope(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tools_gc, "query", _fake_query([], calls))
        for q in ("", "   "):
            assert tools_gc.guide_cases_search(q) == {
                "results": [], "count": 0, "truncated": False}
        assert calls == []

    def test_rows_branded_and_provenance_free(self, monkeypatch):
        rows = [_row("指导性案例", source_id="1"), _row("指导性案例之二", source_id="2")]
        monkeypatch.setattr(tools_gc, "query", _fake_query(rows))
        out = tools_gc.guide_cases_search("指导性案例")
        assert out["count"] == 2
        for r in out["results"]:
            assert r["brand"] == "finddata"
            assert not (_FORBIDDEN & set(r))
            assert "body" not in r
            # full search-row metadata contract
            assert {"source_id", "case_no", "doc_type", "title",
                    "listed_at", "publish_at", "source",
                    "reference_status"} <= set(r)

    def test_like_metacharacters_escaped(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tools_gc, "query", _fake_query([], calls))
        tools_gc.guide_cases_search("50%_of")
        p = calls[0]["params"]
        assert p["exact"] == "50\\%\\_of"
        assert p["prefix"] == "50\\%\\_of%"
        assert p["pattern"] == "%50\\%\\_of%"

    def test_dates_cast_to_text_in_sql(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tools_gc, "query", _fake_query([], calls))
        tools_gc.guide_cases_search("指导性案例")
        assert "::date::text AS listed_at" in calls[0]["sql"]
        assert "::date::text AS publish_at" in calls[0]["sql"]

    def test_doc_type_filter_both_values(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tools_gc, "query", _fake_query([], calls))
        for dt in ("case", "notice"):
            calls.clear()
            tools_gc.guide_cases_search("指导性案例", doc_type=dt)
            assert calls[0]["dbname"] == "law_db"
            assert calls[0]["params"]["doc_type"] == dt
            assert "AND doc_type = :doc_type" in calls[0]["sql"]

    def test_doc_type_invalid_rejected_without_query(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tools_gc, "query", _fake_query([], calls))
        for dt in ("判决书", "CASE", "guidance"):
            assert tools_gc.guide_cases_search("指导性案例", doc_type=dt) == {
                "status": "invalid_filter", "field": "doc_type",
                "valid_values": ["case", "notice"], "brand": "finddata"}
        assert calls == []

    def test_reference_status_filter_hits_discontinued_row(self, monkeypatch):
        rows = [
            _row("指导性案例九号", source_id="9", case_no=9,
                 reference_status="不再参照",
                 reference_note="法〔2020〕347号"),
            _row("指导性案例十号", source_id="10", case_no=10,
                 reference_status="有效"),
        ]

        def filtering(dbname, sql, params=None):
            # mimic the SQL: only the requested reference_status survives
            want = params.get("reference_status")
            return [r for r in rows if not want or r["reference_status"] == want]

        monkeypatch.setattr(tools_gc, "query", filtering)
        out = tools_gc.guide_cases_search("指导性案例", reference_status="不再参照")
        assert out["count"] == 1
        assert out["results"][0]["source_id"] == "9"
        assert out["results"][0]["reference_status"] == "不再参照"

    def test_reference_status_filter_clause_in_sql(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tools_gc, "query", _fake_query([], calls))
        tools_gc.guide_cases_search("指导性案例", reference_status="有效")
        assert calls[0]["params"]["reference_status"] == "有效"
        assert "AND reference_status = :reference_status" in calls[0]["sql"]

    def test_reference_status_invalid_rejected_without_query(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tools_gc, "query", _fake_query([], calls))
        for rs in ("失效", "effective", "valid"):
            assert tools_gc.guide_cases_search("指导性案例", reference_status=rs) == {
                "status": "invalid_filter", "field": "reference_status",
                "valid_values": ["不再参照", "有效"], "brand": "finddata"}
        assert calls == []

    def test_no_filter_clauses_without_filters(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tools_gc, "query", _fake_query([], calls))
        tools_gc.guide_cases_search("指导性案例")
        assert "doc_type" not in calls[0]["params"]
        assert "reference_status" not in calls[0]["params"]
        assert "AND doc_type" not in calls[0]["sql"]
        assert "AND reference_status" not in calls[0]["sql"]

    def test_domain_unavailable_passthrough(self, monkeypatch):
        fail = {"status": "domain_unavailable", "detail": "OperationalError"}
        monkeypatch.setattr(tools_gc, "query", _fake_query(fail))
        assert tools_gc.guide_cases_search("指导性案例") == fail
        assert tools_gc.guide_cases_search(
            "指导性案例", doc_type="case", reference_status="有效") == fail


class TestGuideCasesReadUnit:
    def test_full_sections_case_returned_branded(self, monkeypatch):
        monkeypatch.setattr(tools_gc, "query", _fake_query([_row()]))
        out = tools_gc.guide_cases_read("490521")
        for key in _SECTIONS:
            assert out[key]
        assert out["body"] == "FULL-BODY"
        assert out["reference_status"] == "有效"
        assert out["source_url"] == "https://example.court/490521"
        assert out["brand"] == "finddata"
        assert not (_FORBIDDEN & set(out))

    def test_notice_missing_sections_omitted_body_present(self, monkeypatch):
        row = _row("关于发布第XX批指导性案例的通知", doc_type="notice", case_no=None,
                   **{k: None for k in _SECTIONS})
        monkeypatch.setattr(tools_gc, "query", _fake_query([row]))
        out = tools_gc.guide_cases_read("490999")
        for key in _SECTIONS:
            assert key not in out, f"unpublished section {key} must be absent"
        assert out["body"] == "FULL-BODY"
        assert out["doc_type"] == "notice"
        assert out["brand"] == "finddata"

    def test_discontinued_case_readable_with_note(self, monkeypatch):
        row = _row("指导性案例九号", case_no=9, reference_status="不再参照",
                   reference_note="法〔2020〕347号")
        monkeypatch.setattr(tools_gc, "query", _fake_query([row]))
        out = tools_gc.guide_cases_read("9")
        assert out["reference_status"] == "不再参照"
        assert out["reference_note"] == "法〔2020〕347号"

    def test_not_found_for_empty_row_result(self, monkeypatch):
        monkeypatch.setattr(tools_gc, "query", _fake_query([]))
        assert tools_gc.guide_cases_read("424242") == {
            "status": "not_found", "brand": "finddata"}

    def test_case_id_bound_as_param(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tools_gc, "query", _fake_query([], calls))
        tools_gc.guide_cases_read("490521")
        assert calls[0]["params"] == {"case_id": "490521"}
        assert "WHERE source_id = :case_id" in calls[0]["sql"]

    def test_domain_unavailable_passthrough(self, monkeypatch):
        fail = {"status": "domain_unavailable", "detail": "TimeoutError"}
        monkeypatch.setattr(tools_gc, "query", _fake_query(fail))
        assert tools_gc.guide_cases_read("490521") == fail


@pytest.mark.skipif(not os.environ.get("FDBIZ_DOMAIN_PG_URL"),
                    reason="FDBIZ_DOMAIN_PG_URL not set; federated PG unreachable")
class TestGuideCasesIntegration:
    def test_search_partial_title(self):
        out = tools_gc.guide_cases_search("案例")
        assert out["count"] > 0
        for r in out["results"]:
            assert "案例" in (r["title"] or "")
            assert "body" not in r
            assert r["brand"] == "finddata"
            assert "reference_status" in r
            assert not (_FORBIDDEN & set(r))

    def test_search_ranking_non_decreasing(self):
        out = tools_gc.guide_cases_search("案例", limit=200)
        q = "案例"
        ranks = []
        for r in out["results"]:
            t = (r["title"] or "").lower()
            ranks.append(0 if t == q else 1 if t.startswith(q) else 2)
        assert ranks == sorted(ranks)

    def test_doc_type_filter_narrows(self):
        broad = tools_gc.guide_cases_search("指导性案例", limit=200)
        notices = tools_gc.guide_cases_search(
            "指导性案例", doc_type="notice", limit=200)
        assert notices["count"] > 0
        assert notices["count"] <= broad["count"]
        assert all(r["doc_type"] == "notice" for r in notices["results"])

    def test_read_real_case_found_via_search(self):
        out = tools_gc.guide_cases_search("指导性案例", doc_type="case", limit=50)
        assert out["count"] > 0
        read = tools_gc.guide_cases_read(out["results"][0]["source_id"])
        assert read.get("brand") == "finddata"
        assert read.get("body")
        assert not (_FORBIDDEN & set(read))

    def test_read_unknown_id_not_found(self):
        assert tools_gc.guide_cases_read("999999999") == {
            "status": "not_found", "brand": "finddata"}
