"""Tests for the legal-documents domain tools (tools_legal_docs.py).

Unit tests fake the domain query — no network, no environment needed.
"""
from __future__ import annotations

import fd_find_data_business_mcp.tools_legal_docs as tld

_FORBIDDEN = {"source_id", "content_hash", "fetched_at", "loaded_at"}

_VALID_SOURCES = [
    "ccdi-supervisory-regulations",
    "gov-rules-library",
    "mfa-treaty-library",
    "party-regulation-library",
    "rmfyalk-case-library",
]

_SEARCH_ROW_KEYS = {
    "id", "title", "source", "source_label_zh", "doc_type", "status",
    "authority", "language", "published_at", "effective_at",
    "has_content", "brand",
}


def _fake_query(rows, calls=None):
    def q(dbname, sql, params=None):
        if calls is not None:
            calls.append({"dbname": dbname, "sql": sql, "params": params})
        return rows
    return q


def _search_row(title="某管理条例", **over):
    base = {
        "id": 1, "title": title, "source": "gov-rules-library",
        "doc_type": "部门规章", "status": "现行有效", "authority": "某部委",
        "language": "zh", "published_at": "2021-06-01",
        "effective_at": "2021-08-01", "has_content": True, "rank": 2,
        "source_id": "crawler-123", "content_hash": "deadbeef",
        "fetched_at": "2025-01-01 00:00:00",
        "loaded_at": "2025-01-02 00:00:00",
        "source_url": "https://example.gov/rule/1",
        "metadata": {"object_keys": ["a"], "page_no": "第3号"},
        "content": "SECRET-BODY",
        # loaded_at：本表无 created_at，provenance.as_of 取入库时间 loaded_at。
        "loaded_at": "2026-09-08 00:00:00",
    }
    base.update(over)
    return base


def _read_row(has_body=True, **over):
    row = _search_row(**over)
    row.pop("rank")
    row["content_available"] = has_body
    if not has_body:
        row["content"] = None
    return row


class TestLegalDocsSearchUnit:
    def test_ranking_exact_prefix_contains(self, monkeypatch):
        rows = [
            _search_row("关于数据安全的管理规定", id=4),
            _search_row("数据安全管理办法释义", id=1),
            _search_row("数据安全管理办法", id=2),
            _search_row("数据安全管理办法实施手册", id=3),
        ]
        monkeypatch.setattr(tld, "query", _fake_query(rows))
        out = tld.legal_docs_search("数据安全管理办法")
        assert [r["title"] for r in out["results"]] == [
            "数据安全管理办法", "数据安全管理办法实施手册",
            "数据安全管理办法释义", "关于数据安全的管理规定"]

    def test_results_bounded_and_body_free(self, monkeypatch):
        rows = [_search_row(f"条例{i}", id=i) for i in range(5)]
        monkeypatch.setattr(tld, "query", _fake_query(rows))
        out = tld.legal_docs_search("条例", limit=3)
        assert out["count"] == 3
        assert len(out["results"]) == 3
        assert out["truncated"] is True
        for r in out["results"]:
            assert "content" not in r

    def test_not_truncated_when_within_limit(self, monkeypatch):
        rows = [_search_row(f"条例{i}", id=i) for i in range(2)]
        monkeypatch.setattr(tld, "query", _fake_query(rows))
        out = tld.legal_docs_search("条例", limit=2)
        assert out["count"] == 2
        assert out["truncated"] is False

    def test_row_keys_exactly_public_metadata(self, monkeypatch):
        monkeypatch.setattr(tld, "query", _fake_query([_search_row()]))
        out = tld.legal_docs_search("管理条例")
        # provenance 是本变更新增的溯源三件套（与 lex 侧同形状），故行键集含它。
        assert set(out["results"][0]) == _SEARCH_ROW_KEYS | {"provenance"}
        prov = out["results"][0]["provenance"]
        assert set(prov) == {"source", "stable_id", "as_of"}
        assert prov["source"] == "law_db.legal_documents"
        # as_of 是完整 ISO（不截日期）——与 lex 侧 tools_corpus 同口径。
        assert prov["as_of"] == "2026-09-08 00:00:00"

    def test_rows_branded_and_provenance_free(self, monkeypatch):
        rows = [_search_row("管理条例甲", id=9), _search_row("管理条例乙", id=10)]
        monkeypatch.setattr(tld, "query", _fake_query(rows))
        out = tld.legal_docs_search("管理条例")
        assert out["count"] == 2
        for r in out["results"]:
            assert r["brand"] == "finddata"
            assert not (_FORBIDDEN & set(r))
            assert "content" not in r
            assert "metadata" not in r

    def test_source_label_zh_mapped(self, monkeypatch):
        rows = [
            _search_row("甲", id=1, source="ccdi-supervisory-regulations"),
            _search_row("乙", id=2, source="gov-rules-library"),
            _search_row("丙", id=3, source="mfa-treaty-library"),
            _search_row("丁", id=4, source="rmfyalk-case-library"),
            _search_row("戊", id=5, source="party-regulation-library"),
        ]
        monkeypatch.setattr(tld, "query", _fake_query(rows))
        out = tld.legal_docs_search("甲乙丙丁戊")
        labels = {r["source"]: r["source_label_zh"] for r in out["results"]}
        assert labels == {
            "ccdi-supervisory-regulations": "纪检监察法规库",
            "gov-rules-library": "政府规章库",
            "mfa-treaty-library": "外交部条约库",
            "rmfyalk-case-library": "人民法院案例库",
            "party-regulation-library": "党内法规库",
        }

    def test_has_content_reflected_as_bool(self, monkeypatch):
        rows = [
            _search_row("有正文条例", id=1, has_content=True),
            _search_row("无正文条例", id=2, has_content=False),
        ]
        monkeypatch.setattr(tld, "query", _fake_query(rows))
        out = tld.legal_docs_search("条例")
        flags = {r["title"]: r["has_content"] for r in out["results"]}
        assert flags == {"有正文条例": True, "无正文条例": False}

    def test_limit_capped_at_200(self, monkeypatch):
        calls = []
        rows = [_search_row(f"法{i}", id=i) for i in range(201)]
        monkeypatch.setattr(tld, "query", _fake_query(rows, calls))
        out = tld.legal_docs_search("法", limit=500)
        assert calls[0]["params"]["lim"] == 201
        assert out["count"] == 200
        assert len(out["results"]) == 200
        assert out["truncated"] is True

    def test_empty_or_whitespace_query_returns_empty_envelope(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tld, "query", _fake_query([], calls))
        for q in ("", "   "):
            assert tld.legal_docs_search(q) == {
                "results": [], "count": 0, "truncated": False, "brand": "finddata"}
        assert calls == []

    def test_source_filter_hits_sql(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tld, "query", _fake_query([], calls))
        tld.legal_docs_search("条例", source="ccdi-supervisory-regulations")
        assert calls[0]["dbname"] == "law_db"
        assert calls[0]["params"]["source"] == "ccdi-supervisory-regulations"
        assert "AND source = :source" in calls[0]["sql"]

    def test_source_filter_value_stripped(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tld, "query", _fake_query([], calls))
        tld.legal_docs_search("条例", source="  gov-rules-library  ")
        assert calls[0]["params"]["source"] == "gov-rules-library"

    def test_source_filter_rejects_unknown_value(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tld, "query", _fake_query([], calls))
        for bad in ("laws", "gov_rules_library", "LIBRARY"):
            assert tld.legal_docs_search("条例", source=bad) == {
                "status": "invalid_filter",
                "field": "source",
                "valid_sources": _VALID_SOURCES,
                "brand": "finddata",
            }
        assert calls == []

    def test_no_source_clause_without_filter(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tld, "query", _fake_query([], calls))
        tld.legal_docs_search("条例")
        assert "source" not in calls[0]["params"]
        assert "AND source = :source" not in calls[0]["sql"]

    def test_doc_type_filter_hits_sql(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tld, "query", _fake_query([], calls))
        tld.legal_docs_search("条约", doc_type="双边条约")
        assert calls[0]["params"]["doc_type"] == "双边条约"
        assert "AND doc_type = :doc_type" in calls[0]["sql"]
        calls.clear()
        tld.legal_docs_search("条约")
        assert "doc_type" not in calls[0]["params"]
        assert "AND doc_type = :doc_type" not in calls[0]["sql"]

    def test_like_metacharacters_escaped(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tld, "query", _fake_query([], calls))
        tld.legal_docs_search("50%_of")
        p = calls[0]["params"]
        assert p["exact"] == "50\\%\\_of"
        assert p["prefix"] == "50\\%\\_of%"
        assert p["pattern"] == "%50\\%\\_of%"

    def test_time_columns_cast_to_text_in_sql(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tld, "query", _fake_query([], calls))
        tld.legal_docs_search("条例")
        sql = calls[0]["sql"]
        assert "published_at::date::text AS published_at" in sql
        assert "effective_at::date::text AS effective_at" in sql

    def test_sql_never_selects_body_or_provenance(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tld, "query", _fake_query([], calls))
        tld.legal_docs_search("条例")
        sql = calls[0]["sql"]
        # loaded_at 已出禁选清单：它是 provenance.as_of 的口径来源，必须读。
        for col in ("source_id", "content_hash", "fetched_at", "metadata"):
            assert col not in sql
        assert "loaded_at" in sql  # as_of 的推导来源
        assert "(content IS NOT NULL) AS has_content" in sql

    def test_domain_unavailable_passthrough(self, monkeypatch):
        fail = {"status": "domain_unavailable", "detail": "OperationalError"}
        monkeypatch.setattr(tld, "query", _fake_query(fail))
        assert tld.legal_docs_search("条例") == fail


class TestLegalDocsReadUnit:
    def test_read_with_content_full_payload(self, monkeypatch):
        monkeypatch.setattr(tld, "query", _fake_query([_read_row(has_body=True)]))
        out = tld.legal_docs_read(1)
        assert out["id"] == 1
        assert out["title"] == "某管理条例"
        assert out["content_available"] is True
        assert out["content"] == "SECRET-BODY"
        assert out["source_url"] == "https://example.gov/rule/1"
        assert out["source_label_zh"] == "政府规章库"
        assert out["brand"] == "finddata"
        assert not (_FORBIDDEN & set(out))
        assert "note" not in out

    def test_read_without_content_no_content_key(self, monkeypatch):
        monkeypatch.setattr(tld, "query", _fake_query([_read_row(has_body=False)]))
        out = tld.legal_docs_read(1)
        assert out["content_available"] is False
        assert "content" not in out  # absent, not an empty string
        assert "正文未抓取" in out["note"]
        assert out["source_url"] == "https://example.gov/rule/1"
        assert out["brand"] == "finddata"
        assert not (_FORBIDDEN & set(out))

    def test_read_without_content_key_set(self, monkeypatch):
        monkeypatch.setattr(tld, "query", _fake_query([_read_row(has_body=False)]))
        out = tld.legal_docs_read(1)
        expected = (_SEARCH_ROW_KEYS - {"has_content"}) | {
            "source_url", "metadata", "content_available", "note", "provenance"}
        assert set(out) == expected

    def test_read_not_found(self, monkeypatch):
        monkeypatch.setattr(tld, "query", _fake_query([]))
        assert tld.legal_docs_read(424242) == {"status": "not_found", "brand": "finddata"}

    def test_read_id_bound_as_param(self, monkeypatch):
        calls = []
        monkeypatch.setattr(tld, "query", _fake_query([], calls))
        tld.legal_docs_read(7)
        assert calls[0]["dbname"] == "law_db"
        assert calls[0]["params"] == {"doc_id": 7}
        assert "WHERE id = :doc_id" in calls[0]["sql"]

    def test_read_strips_object_keys_from_metadata(self, monkeypatch):
        monkeypatch.setattr(tld, "query", _fake_query([
            _read_row(metadata={"object_keys": ["secret"], "page_no": "第3号"}),
        ]))
        out = tld.legal_docs_read(1)
        assert out["metadata"] == {"page_no": "第3号"}

    def test_read_tolerates_null_metadata(self, monkeypatch):
        monkeypatch.setattr(tld, "query", _fake_query([_read_row(metadata=None)]))
        out = tld.legal_docs_read(1)
        assert out["metadata"] is None

    def test_read_no_forbidden_keys_either_state(self, monkeypatch):
        for body in (True, False):
            monkeypatch.setattr(
                tld, "query", _fake_query([_read_row(has_body=body)]))
            out = tld.legal_docs_read(1)
            assert not (_FORBIDDEN & set(out))
            for key in ("source_id", "content_hash", "fetched_at", "loaded_at"):
                assert key not in out

    def test_domain_unavailable_passthrough(self, monkeypatch):
        fail = {"status": "domain_unavailable", "detail": "TimeoutError"}
        monkeypatch.setattr(tld, "query", _fake_query(fail))
        assert tld.legal_docs_read(1) == fail


class TestSearchReadConsistency:
    def test_has_content_flag_matches_read_availability(self, monkeypatch):
        monkeypatch.setattr(tld, "query", _fake_query(
            [_search_row("有正文条例", id=9, has_content=True)]))
        hit = tld.legal_docs_search("有正文条例")["results"][0]
        assert hit["has_content"] is True
        monkeypatch.setattr(tld, "query", _fake_query(
            [_read_row(id=9, has_body=True)]))
        assert tld.legal_docs_read(9)["content_available"] is True

        monkeypatch.setattr(tld, "query", _fake_query(
            [_search_row("无正文条例", id=10, has_content=False)]))
        hit = tld.legal_docs_search("无正文条例")["results"][0]
        assert hit["has_content"] is False
        monkeypatch.setattr(tld, "query", _fake_query(
            [_read_row(id=10, has_body=False)]))
        got = tld.legal_docs_read(10)
        assert got["content_available"] is False
        assert "content" not in got
