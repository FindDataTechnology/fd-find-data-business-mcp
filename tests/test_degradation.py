"""Degradation-isolation tests (change: business-mcp-domain-federation).

5.2 — statement timeout cuts off slow federated queries and the pool stays
usable (run in a subprocess: the timeout is an import-time env constant).
5.1 — with the domain PG black-holed, every domain tool fails soft while the
core tool registrations on the server are untouched.

Live tests: they need FDBIZ_DOMAIN_PG_URL (auto-loaded from the repo .env)
and, for the tail of 5.1, reachability of the Windows data machine.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_env_url() -> str | None:
    url = os.environ.get("FDBIZ_DOMAIN_PG_URL")
    if url:
        return url
    env_path = os.path.join(REPO, ".env")
    if os.path.exists(env_path):
        for line in open(env_path, encoding="utf-8"):
            if line.startswith("FDBIZ_DOMAIN_PG_URL="):
                os.environ["FDBIZ_DOMAIN_PG_URL"] = line.strip().split("=", 1)[1]
                return os.environ["FDBIZ_DOMAIN_PG_URL"]
    return None


pytestmark = pytest.mark.skipif(
    _load_env_url() is None, reason="needs FDBIZ_DOMAIN_PG_URL (repo .env)")


class TestStatementTimeout:
    def test_slow_query_cancelled_and_pool_recovers(self):
        """pg_sleep exceeds the (lowered) statement timeout → cancelled,
        mapped to domain_unavailable, and the next query on the same pool
        succeeds — no leaked/exhausted connections."""
        code = r"""
import json, os, time
os.environ["FDBIZ_DOMAIN_STATEMENT_TIMEOUT_MS"] = "1500"
from fd_find_data_business_mcp.domain import query
t0 = time.monotonic()
slow = query("law_db", "SELECT pg_sleep(8) AS x")
t_slow = time.monotonic() - t0
t1 = time.monotonic()
ok = query("law_db", "SELECT count(*) AS n FROM laws")
t_ok = time.monotonic() - t1
print(json.dumps({
    "slow": slow, "t_slow": round(t_slow, 2),
    "ok": ok, "t_ok": round(t_ok, 2),
}))
"""
        r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                           text=True, cwd=REPO, timeout=60)
        assert r.returncode == 0, r.stderr[-2000:]
        out = json.loads(r.stdout.strip().splitlines()[-1])
        # Cancelled by the server-side timeout, not the client's patience.
        assert out["slow"]["status"] == "domain_unavailable"
        assert out["t_slow"] < 8, "query must be cut off around the 1.5s budget"
        # Pool immediately usable: no connection leaked by the cancelled stmt.
        assert isinstance(out["ok"], list) and out["ok"][0]["n"] > 0
        assert out["t_ok"] < 10


class TestFailSoftEndToEnd:
    def test_domain_tools_fail_soft_core_untouched(self, monkeypatch):
        """Black-hole the domain PG: all four domain tools return
        domain_unavailable promptly; the FastMCP server object still exposes
        the five core tools; and if the canonical store is reachable, a core
        call still succeeds in the same process."""
        import fd_find_data_business_mcp.domain as domain
        from fd_find_data_business_mcp import (server, tools_law,
                                               tools_yearbook)

        monkeypatch.setenv("FDBIZ_DOMAIN_PG_URL",
                           "postgresql://fdbiz_ro:x@100.64.0.99:5432/postgres")
        domain.dispose_all()
        try:
            t0 = time.monotonic()
            results = {
                "yearbook_search_indicators":
                    tools_yearbook.yearbook_search_indicators("生产总值"),
                "yearbook_read": tools_yearbook.yearbook_read(1, "北京市"),
                "law_search": tools_law.law_search("民法典"),
                "law_read": tools_law.law_read(1),
            }
            elapsed = time.monotonic() - t0
            for name, res in results.items():
                assert res.get("status") == "domain_unavailable", (name, res)
            # Four sequential refused/black-holed calls inside the 5s connect
            # budget each: generous bound, still "prompt" in MCP terms.
            assert elapsed < 25, elapsed

            tool_names = {t.name for t in _registered_tools(server.mcp)}
            assert {"read", "read_range", "list_concepts", "ai_search",
                    "graph_search"} <= tool_names

            # Core path untouched when canonical store is reachable; skip
            # quietly when it is not (dev machine without tunnel).
            if os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL"):
                core = server.list_concepts()
                assert isinstance(core, list)
        finally:
            # monkeypatch restores the env, but engines cached under the bad
            # URL survive — drop them so later tests start clean.
            domain.dispose_all()


def _registered_tools(mcp):
    """FastMCP version-agnostic tool listing (3.x: async list_tools)."""
    import asyncio
    try:
        tools = mcp.list_tools()
    except AttributeError:  # fastmcp < 2.7
        return mcp._tool_manager.list_tools()
    if hasattr(tools, "__await__"):
        tools = asyncio.run(tools)
    return tools
