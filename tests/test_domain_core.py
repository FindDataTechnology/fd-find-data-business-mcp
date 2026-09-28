"""Unit tests for the domain federation core (domain.py).

No network, no live PG: engines are faked or pointed at a refusing socket.
Live-PG behaviour is covered by tests/test_domain_integration.py.
"""
from __future__ import annotations

import os
import time

import pytest
import sqlalchemy

import fd_find_data_business_mcp.domain as domain


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    # Never touch a real env; each test sets its own URL or none.
    monkeypatch.delenv("FDBIZ_DOMAIN_PG_URL", raising=False)
    domain.dispose_all()
    yield
    domain.dispose_all()


class TestNotConfigured:
    def test_query_without_env_returns_unavailable(self):
        assert domain.query("law_db", "SELECT 1") == {
            "status": "domain_unavailable", "detail": "not_configured"}

    def test_configured_reflects_env(self, monkeypatch):
        assert domain.configured() is False
        monkeypatch.setenv("FDBIZ_DOMAIN_PG_URL", "postgresql://u:p@127.0.0.1:1/x")
        assert domain.configured() is True


class TestFailSoft:
    def test_refused_connection_maps_to_unavailable(self, monkeypatch):
        # Port 1 on localhost: connection refused — fails fast, no tailnet.
        monkeypatch.setenv("FDBIZ_DOMAIN_PG_URL",
                           "postgresql://u:p@127.0.0.1:1/postgres")
        t0 = time.monotonic()
        r = domain.query("law_db", "SELECT 1 AS x")
        assert r["status"] == "domain_unavailable"
        assert "detail" in r
        # Prompt: far under the 5s connect budget on a refused socket.
        assert time.monotonic() - t0 < 3

    def test_query_never_raises_on_bad_sql(self, monkeypatch):
        # Fake engine whose execute blows up with a non-SQLAlchemy error to
        # prove the broad catch (psycopg2 can leak raw errors).
        monkeypatch.setenv("FDBIZ_DOMAIN_PG_URL",
                           "postgresql://u:p@127.0.0.1:1/postgres")

        class Boom:
            def connect(self):
                raise UnicodeDecodeError("utf-8", b"\xd6", 0, 1, "boom")

        captured = {}
        monkeypatch.setattr(
            domain.sqlalchemy, "create_engine",
            lambda *a, **k: (captured.setdefault("engine", Boom())))
        r = domain.query("law_db", "SELECT 1")
        assert r == {"status": "domain_unavailable",
                     "detail": "UnicodeDecodeError"}


class TestEngineLifecycle:
    def _fake_url_env(self, monkeypatch):
        monkeypatch.setenv("FDBIZ_DOMAIN_PG_URL",
                           "postgresql://u:p@127.0.0.1:1/postgres")

    def test_engines_lazy_and_reused(self, monkeypatch):
        self._fake_url_env(monkeypatch)
        calls = []
        real = sqlalchemy.create_engine

        def spy(url, **k):
            calls.append(1)
            return real("sqlite://")

        monkeypatch.setattr(domain.sqlalchemy, "create_engine", spy)
        domain.get_engine("law_db")
        domain.get_engine("law_db")
        domain.get_engine("law_db")
        assert len(calls) == 1, "engine must be created once and reused"

    def test_engine_per_database(self, monkeypatch):
        self._fake_url_env(monkeypatch)
        made = []
        real = sqlalchemy.create_engine

        def spy(url, **k):
            made.append(str(url))
            return real("sqlite://")

        monkeypatch.setattr(domain.sqlalchemy, "create_engine", spy)
        domain.get_engine("law_db")
        domain.get_engine("yearbook_catalog")
        assert len(made) == 2
        assert any("law_db" in u for u in made)
        assert any("yearbook_catalog" in u for u in made)

    def test_recovery_after_dispose_rebuilds_engine(self, monkeypatch):
        self._fake_url_env(monkeypatch)
        made = []
        real = sqlalchemy.create_engine

        def spy(url, **k):
            made.append(1)
            return real("sqlite://")

        monkeypatch.setattr(domain.sqlalchemy, "create_engine", spy)
        domain.get_engine("law_db")
        domain.dispose_all()
        domain.get_engine("law_db")
        assert len(made) == 2, "dispose must force a rebuild on next use"

    def test_connect_args_carry_timeouts(self, monkeypatch):
        self._fake_url_env(monkeypatch)
        captured = {}
        real = sqlalchemy.create_engine

        def spy(url, **k):
            captured.update(k)
            return real("sqlite://")

        monkeypatch.setattr(domain.sqlalchemy, "create_engine", spy)
        domain.get_engine("law_db")
        ca = captured["connect_args"]
        assert ca["connect_timeout"] == domain.CONNECT_TIMEOUT_S
        assert f"statement_timeout={domain.STATEMENT_TIMEOUT_MS}" in ca["options"]
        assert captured["pool_pre_ping"] is True


class TestRegistryLookup:
    """registry_semantic_codes: one anchor lookup, verified-only, fail-soft."""

    def test_semantic_codes_without_env_return_empty(self):
        # _isolate deletes FDBIZ_DOMAIN_PG_URL -> query is not_configured.
        assert domain.registry_semantic_codes(
            "world_bank", "indicator", "code", ["NY.GDP.PCAP.CD"]) == {}

    def test_empty_or_blank_codes_short_circuit_without_query(self, monkeypatch):
        calls = []
        monkeypatch.setattr(domain, "query",
                            lambda *a, **k: calls.append(1) or [])
        assert domain.registry_semantic_codes("x", "y", "z", []) == {}
        assert domain.registry_semantic_codes("x", "y", "z", ["", None]) == {}
        assert calls == []

    def test_unavailable_registry_maps_to_empty(self, monkeypatch):
        monkeypatch.setattr(
            domain, "query",
            lambda *a, **k: {"status": "domain_unavailable",
                             "detail": "OperationalError"})
        assert domain.registry_semantic_codes("x", "y", "z", ["a"]) == {}

    def test_maps_anchor_and_casts_dedupes_codes(self, monkeypatch):
        captured = {}

        def fake_query(dbname, sql, params):
            captured.update(dbname=dbname, sql=sql, params=params)
            return [{"native_code": "12209", "semantic_code": "YB.GDP"},
                    {"native_code": "2", "semantic_code": "YB.OTHER"}]

        monkeypatch.setattr(domain, "query", fake_query)
        out = domain.registry_semantic_codes(
            "yearbook_catalog", "indicators", "id",
            [12209, "12209", 2, "", None])
        assert out == {"12209": "YB.GDP", "2": "YB.OTHER"}
        # The lookup targets the registry database with the full anchor.
        assert captured["dbname"] == domain.REGISTRY_DB == "fd_open_data"
        assert "registry_entries" in captured["sql"]
        assert "verified" in captured["sql"]
        assert captured["params"] == {"sdb": "yearbook_catalog",
                                      "stab": "indicators", "scol": "id",
                                      "codes": ["12209", "2"]}

    def test_null_semantic_code_rows_are_dropped(self, monkeypatch):
        monkeypatch.setattr(domain, "query", lambda *a, **k: [
            {"native_code": "a", "semantic_code": None}])
        assert domain.registry_semantic_codes("x", "y", "z", ["a"]) == {}
