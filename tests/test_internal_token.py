"""The service-to-service auth lane (registry-transparent-read-and-scope).

business-mcp in production is JWT-gated (FDBIZ_JWKS_URI). fd-open-data-mcp's
transparent federation reads call it directly, so the gate must admit a named
shared secret (FDBIZ_INTERNAL_TOKEN) alongside Logto JWTs — the caller sends
it as FD_MCP_BUSINESS_TOKEN. These tests boot the real HTTP server with the
gate on and prove: internal token → 200 tool call; wrong/no token → 401.
No Logto needed — the internal path short-circuits before any JWKS fetch.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def gated_server():
    port = _free_port()
    env = {
        **os.environ,
        # JWT gate ON, but the JWKS endpoint does not exist: any Logto path
        # would fail; only the internal token can pass.
        "FDBIZ_JWKS_URI": "https://auth.invalid/oidc/jwks",
        "FDBIZ_INTERNAL_TOKEN": "svc-secret-123",
        "FDBIZ_DOMAIN_PG_URL": "",
        "FD_OPEN_DATA_MCP_DATABASE_URL": "sqlite:///:memory:",
    }
    cli = Path(sys.executable).parent / "fd-find-data-business-mcp"
    proc = subprocess.Popen(
        [str(cli), "serve", "--transport", "http",
         "--host", "127.0.0.1", "--port", str(port)],
        cwd=REPO, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}/mcp"
    deadline = time.time() + 30
    ready = False
    while time.time() < deadline and not ready:
        try:
            urllib.request.urlopen(base, timeout=2)
        except urllib.error.HTTPError:
            ready = True  # any HTTP answer (401 etc.) means the server is up
        except Exception:
            if proc.poll() is not None:
                break
            time.sleep(0.4)
    if not ready:
        proc.kill()
        out = proc.stdout.read()[-500:] if proc.stdout else ""
        raise RuntimeError(f"gated business-mcp did not come up: {out}")
    yield base
    proc.kill()
    proc.wait()


def _initialize(base: str, token: str | None) -> tuple[int, str]:
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "0"}},
    }).encode()
    req = urllib.request.Request(base, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()[:200]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


def test_internal_token_passes_the_gate(gated_server):
    status, body = _initialize(gated_server, "svc-secret-123")
    assert status == 200, body
    assert "serverInfo" in body or "result" in body


def test_wrong_token_is_401(gated_server):
    status, _ = _initialize(gated_server, "wrong-secret")
    assert status == 401


def test_missing_token_is_401(gated_server):
    status, _ = _initialize(gated_server, None)
    assert status == 401
