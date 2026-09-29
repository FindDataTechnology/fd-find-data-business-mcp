"""Scope-parameter pass-through on the domain search tools (indicator-scope,
shared-library design D4): unknown scope errors explicitly, a scope excluding
the tool's source yields an explainable empty result, a permitted scoped call
is disclosed + counted — and an unscoped call is byte-identical to before.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import text

os.environ.setdefault("FDBIZ_JWKS_URI", "")  # no auth in unit tests

from fd_open_data_mcp import db as dbmod  # noqa: E402
from fd_open_data_mcp.models import Base, ScopeStat  # noqa: E402

import fd_find_data_business_mcp.server as biz  # noqa: E402

REGISTRY_DDL = """
CREATE TABLE registry_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_db VARCHAR(64), source_table VARCHAR(128), source_column VARCHAR(128),
  native_code VARCHAR(128), semantic_code VARCHAR(128),
  name_zh VARCHAR(255), name_en VARCHAR(255), unit VARCHAR(64),
  frequency VARCHAR(32), domain VARCHAR(64), verified INTEGER DEFAULT 0
)
"""


@pytest.fixture
def scoped_db(tmp_path, monkeypatch):
    monkeypatch.setenv("FD_OPEN_DATA_MCP_DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    dbmod.reset_database()
    db = dbmod.get_database()
    Base.metadata.create_all(db.engine)
    s = db.get_session()
    s.execute(text(REGISTRY_DDL))
    s.execute(text(
        "INSERT INTO registry_entries (source_db, source_table, source_column, "
        "native_code, semantic_code, name_zh, name_en, unit, frequency, domain, verified) "
        "VALUES ('yearbook_catalog','t','v','10401','yb.total_population','n','n',"
        "'u','yearly','人口',1)"))
    s.execute(text(
        "INSERT INTO registry_entries (source_db, source_table, source_column, "
        "native_code, semantic_code, name_zh, name_en, unit, frequency, domain, verified) "
        "VALUES ('world_bank','t','v','SP.POP.TOTL','wb.population_total','n','n',"
        "'u','yearly','人口',1)"))
    s.commit()
    s.close()
    yield db
    dbmod.reset_database()


def _impl_boom(*a, **k):
    raise AssertionError("domain impl must not run when the gate rejects")


def test_unknown_scope_errors_explicitly(scoped_db, monkeypatch):
    monkeypatch.setattr(biz, "yearbook_search_indicators_impl", _impl_boom, raising=False)
    out = biz.yearbook_search_indicators("人口", scope="ghost")
    assert out["error"] == "unknown_scope"
    assert out["scope"] == "ghost"


def test_source_outside_scope_is_explainable_empty(scoped_db, monkeypatch):
    """A wb-only scope against the yearbook tool: empty by constraint, and the
    domain impl never runs."""
    s = scoped_db.get_session()
    from fd_open_data_mcp import scoping
    scoping.scope_create(s, "wb_only", {"source_dbs": ["world_bank"]})
    s.close()

    monkeypatch.setattr(
        biz, "_scope_gate_impl_guard", True, raising=False)  # marker only
    import fd_find_data_business_mcp.tools_yearbook as ty
    monkeypatch.setattr(ty, "yearbook_search_indicators", _impl_boom)
    monkeypatch.setattr(biz, "_strip", lambda x: x)  # pass-through for assertions

    out = biz.yearbook_search_indicators("人口", scope="wb_only")
    assert out["count"] == 0 and out["results"] == []
    assert out["status"] == "out_of_scope"
    assert out["scope"]["name"] == "wb_only"
    assert "outside the scope" in out["note"]


def test_admitted_scoped_call_is_disclosed_and_counted(scoped_db, monkeypatch):
    from fd_open_data_mcp import scoping
    s = scoped_db.get_session()
    scoping.scope_create(s, "yb_only", {"source_dbs": ["yearbook_catalog"]})
    s.close()

    import fd_find_data_business_mcp.tools_yearbook as ty
    payload = {"results": [{"id": 1, "name": "年末总人口", "brand": "finddata"}],
               "count": 1, "truncated": False}

    def fake_impl(query, limit):
        return dict(payload)

    monkeypatch.setattr(ty, "yearbook_search_indicators", fake_impl)
    monkeypatch.setattr(biz, "_strip", lambda x: x)

    out = biz.yearbook_search_indicators("人口", scope="yb_only")
    assert out["count"] == 1
    assert out["scope"]["name"] == "yb_only"

    s = scoped_db.get_session()
    stats = s.query(ScopeStat).filter_by(scope_name="yb_only").all()
    s.close()
    assert len(stats) == 1
    assert stats[0].calls == 1 and stats[0].results_returned == 1


def test_unscoped_call_unchanged(scoped_db, monkeypatch):
    import fd_find_data_business_mcp.tools_yearbook as ty
    monkeypatch.setattr(ty, "yearbook_search_indicators",
                        lambda q, limit: {"results": [], "count": 0, "truncated": False})
    out = biz.yearbook_search_indicators("人口")
    assert out == {"results": [], "count": 0, "truncated": False}
    assert "scope" not in out
