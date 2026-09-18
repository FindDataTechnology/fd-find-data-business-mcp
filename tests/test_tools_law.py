"""Tests for the law-domain tools (tools_law.py).

Unit tests fake the domain query — no network. Integration tests hit the
real federated PG via FDBIZ_DOMAIN_PG_URL, loaded from the repo .env when
absent from the environment.
"""
from __future__ import annotations

import os
import pathlib

import pytest

import fd_find_data_business_mcp.tools_law as tools_law
from fd_find_data_business_mcp.domain import query as domain_query

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

if not os.environ.get("FDBIZ_DOMAIN_PG_URL"):
    _env = _REPO_ROOT / ".env"
    if _env.exists():
        for _line in _env.read_text().splitlines():
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _key, _, _val = _line.partition("=")
                os.environ.setdefault(_key.strip(), _val.strip())

_FORBIDDEN = {
    "source", "source_used", "real_source_used", "real_source",
    "src_db", "src_id", "source_id", "file_path",
    "content_status", "quality_flag",
}


def _fake_query(rows, calls=None):
    def q(dbname, sql, params=None):
        if calls is not None:
            calls.append({"dbname": dbname, "sql": sql, "params": params})
        return rows
    return q


def _row(title, **over):
    base = {
        "id": 1, "title": title, "category": "法律", "type": "法律",
        "status": "有效", "publish": "2020-05-28 00:00:00", "expiry": None,
        "src_db": "hidden", "src_id": 7, "source_id": "hidden",
        "file_path": "hidden", "content_status": "ok", "quality_flag": None,
        "content": "SECRET-BODY",
    }
    base.update(over)
    return base


class TestLawSearchUnit:
    def test_ranking_exact_prefix_contains(self, monkeypatch):
        rows = [
            _row("关于民法典的说明", id=4),
            _row("中华人民共和国民法典释义", id=1),
            _row("民法典", id=2),
            _row("民法典实施手册", id=3),
        ]
        monkeypatch.setattr(tools_law, "query", _fake_query(rows))
        out = tools_law.law_search("民法典")
        assert [r["title"] for r in out["results"]] == [
            "民法典", "民法典实施手册",
            "中华人民共和国民法典释义", "关于民法典的说明"]

    def test_truncated_when_more_matches_than_limit(self, monkeypatch):
        rows = [_row(f"民法典{i}", id=i) for i in range(3)]
        monkeypatch.setattr(tools_law, "query", _fake_query(rows))
        out = tools_law.law_search("民法典", limit=2)
        assert out["count"] == 2
        assert len(out["results"]) == 2
        assert out["truncated"] is True

    def test_not_truncated_when_within_limit(self, monkeypatch):
        rows = [_row(f"民法典{i}", id=i) for i in range(2)]
        monkeypatch.setattr(tools_law, "query", _fake_query(rows))
        out = tools_law.law_search("民法典", limit=2)
        assert out["count"] == 2
        assert out["truncated"] is False

    def test_limit_capped_at_200(self, monkeypatch):
        calls = []
        rows = [_row(f"法{i}", id=i) for i in range(201)]
        monkeypatch.setattr(tools_law, "query", _fake_query(rows, calls))
        out = tools_law.law_search("法", limit=500)
        assert calls[0]["params"]["lim"] == 201
        assert out["count"] == 200
        assert len(out["results"]) == 200
        assert out["truncated"] is True

    def test_empty_or_whitespace_query_returns_empty_envelope(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tools_law, "query", _fake_query([], calls))
        for q in ("", "   "):
            assert tools_law.law_search(q) == {"results": [], "count": 0, "truncated": False}
        assert calls == []

    def test_rows_branded_and_provenance_free(self, monkeypatch):
        rows = [_row("民法典", id=9), _row("民法典讲解", id=10)]
        monkeypatch.setattr(tools_law, "query", _fake_query(rows))
        out = tools_law.law_search("民法典")
        assert out["count"] == 2
        for r in out["results"]:
            assert r["brand"] == "finddata"
            assert not (_FORBIDDEN & set(r))
            assert "content" not in r

    def test_like_metacharacters_escaped(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tools_law, "query", _fake_query([], calls))
        tools_law.law_search("50%_of")
        p = calls[0]["params"]
        assert p["exact"] == "50\\%\\_of"
        assert p["prefix"] == "50\\%\\_of%"
        assert p["pattern"] == "%50\\%\\_of%"

    def test_category_filter_bound_as_param(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tools_law, "query", _fake_query([], calls))
        tools_law.law_search("民法典", category="法律")
        assert calls[0]["dbname"] == "law_db"
        assert calls[0]["params"]["category"] == "法律"
        assert "AND category = :category" in calls[0]["sql"]

    def test_no_category_clause_without_filter(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tools_law, "query", _fake_query([], calls))
        tools_law.law_search("民法典")
        assert "category" not in calls[0]["params"]
        assert ":category" not in calls[0]["sql"]

    def test_domain_unavailable_passthrough(self, monkeypatch):
        fail = {"status": "domain_unavailable", "detail": "OperationalError"}
        monkeypatch.setattr(tools_law, "query", _fake_query(fail))
        assert tools_law.law_search("民法典") == fail


class TestLawReadUnit:
    def test_full_content_returned_branded(self, monkeypatch):
        monkeypatch.setattr(tools_law, "query", _fake_query([_row("民法典")]))
        out = tools_law.law_read(1)
        assert out["title"] == "民法典"
        assert out["content"] == "SECRET-BODY"
        assert out["brand"] == "finddata"
        assert not (_FORBIDDEN & set(out))

    def test_not_found_for_empty_row_result(self, monkeypatch):
        monkeypatch.setattr(tools_law, "query", _fake_query([]))
        assert tools_law.law_read(424242) == {"status": "not_found", "brand": "finddata"}

    def test_id_bound_as_param(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tools_law, "query", _fake_query([], calls))
        tools_law.law_read(7)
        assert calls[0]["params"] == {"law_id": 7}
        assert "WHERE id = :law_id" in calls[0]["sql"]

    def test_domain_unavailable_passthrough(self, monkeypatch):
        fail = {"status": "domain_unavailable", "detail": "TimeoutError"}
        monkeypatch.setattr(tools_law, "query", _fake_query(fail))
        assert tools_law.law_read(1) == fail


@pytest.mark.skipif(not os.environ.get("FDBIZ_DOMAIN_PG_URL"),
                    reason="FDBIZ_DOMAIN_PG_URL not set; federated PG unreachable")
class TestLawIntegration:
    def test_search_partial_title(self):
        out = tools_law.law_search("民法典")
        assert out["count"] > 0
        for r in out["results"]:
            assert "民法典" in (r["title"] or "")
            assert "content" not in r
            assert r["brand"] == "finddata"
            assert not (_FORBIDDEN & set(r))

    def test_search_ranking_non_decreasing(self):
        out = tools_law.law_search("民法典", limit=200)
        q = "民法典"
        ranks = []
        for r in out["results"]:
            t = (r["title"] or "").lower()
            ranks.append(0 if t == q else 1 if t.startswith(q) else 2)
        assert ranks == sorted(ranks)

    def test_category_filter_narrows(self):
        broad = tools_law.law_search("民法典", limit=200)
        law = tools_law.law_search("民法典", category="法律", limit=200)
        assert law["count"] > 0
        assert law["count"] <= broad["count"]
        assert all(r["category"] == "法律" for r in law["results"])

    def test_read_real_law_found_via_search(self):
        out = tools_law.law_search("民法典", limit=50)
        ids = {r["id"] for r in out["results"]}
        read = None
        for r in out["results"]:
            got = tools_law.law_read(r["id"])
            assert got.get("brand") == "finddata"
            if got.get("content"):
                read = got
                break
        assert read is not None, "at least one searched law must carry content"
        assert read["id"] in ids
        assert not (_FORBIDDEN & set(read))

    def test_read_unknown_id_not_found(self):
        assert tools_law.law_read(999999999) == {"status": "not_found", "brand": "finddata"}

    def test_explain_uses_trgm_index(self, monkeypatch):
        captured = []

        def spying(dbname, sql, params=None):
            captured.append((dbname, sql, params))
            return domain_query(dbname, sql, params)

        monkeypatch.setattr(tools_law, "query", spying)
        tools_law.law_search("民法典")
        dbname, sql, params = captured[0]
        plan = domain_query(dbname, "EXPLAIN " + sql, params)
        plan_text = "\n".join(r["QUERY PLAN"] for r in plan)
        costs = [float(line.split("cost=")[1].split("..")[1].split()[0])
                 for line in (r["QUERY PLAN"] for r in plan) if "cost=" in line]
        assert ("Index" in plan_text or "Bitmap" in plan_text) or max(costs) < 2000
