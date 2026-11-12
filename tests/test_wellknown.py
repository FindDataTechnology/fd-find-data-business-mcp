"""线自描述端点 ``/.well-known/line-manifest.json`` 的判据（change converge-line-identity-and-keys 1.4）。

判据都对着**这个端点自己**，不拿邻近信号冒充（同族教训：``/health`` 绿不等于清单在）：

1. 成功响应与仓内 ``line-manifest.json`` **逐字节相同**——端点是投影，不是第二真源；
2. ``content-type`` 是 JSON（``application/json; charset=utf-8``）；
3. ``Cache-Control`` 只在 200 上出现，404/500 不带——别把"暂时没有"缓存成长期事实；
4. ``register()`` 挂的就是约定路径常量 ``WELL_KNOWN_PATH``；
5. ``LINE_MANIFEST_PATH`` 生效且封顶候选链；
6. 清单缺失 = 404、存在但坏 = 500，两者不能混（404 会让调用方以为"本来就没发布"）；
7. 真装配（模块级 ``mcp`` + ``http_app()``）请求该路径 200 且字节相同。

本仓没有共享 bearer 门（门是 fastmcp 只挂在 ``/mcp`` 上的 ``JWTVerifier``），
``custom_route`` 天然公开，所以这里没有 ``EXEMPT_PATHS`` 可断言；第 7 条用真装配
证明这条路由确实可达。
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest

os.environ.setdefault("FDBIZ_JWKS_URI", "")  # no auth in unit tests

from fd_find_data_business_mcp import wellknown  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO_MANIFEST = REPO_ROOT / "line-manifest.json"


def test_serves_repo_manifest_byte_for_byte():
    """原样回吐：端点的字节 == 仓内真源的字节（不是"解析后再 dump"）。"""
    assert REPO_MANIFEST.exists(), f"仓内真源不见了：{REPO_MANIFEST}"
    status, body, headers = wellknown.manifest_response()
    assert status == 200
    assert body == REPO_MANIFEST.read_bytes()
    assert json.loads(body)["line"]  # 至少是个带 line 的清单，不是空壳


def test_register_mounts_exactly_the_agreed_path():
    """``register()`` 挂的就是约定路径常量——两处不许各写各的。"""
    pytest.importorskip("starlette")

    class _FakeMCP:
        def __init__(self):
            self.routes: list[tuple[str, object]] = []

        def custom_route(self, path, methods=None):
            def deco(fn):
                self.routes.append((path, fn))
                return fn

            return deco

    mcp = _FakeMCP()
    wellknown.register(mcp)
    assert [p for p, _ in mcp.routes] == [wellknown.WELL_KNOWN_PATH]
    assert wellknown.WELL_KNOWN_PATH == "/.well-known/line-manifest.json"


def test_env_override_caps_the_candidate_chain(tmp_path, monkeypatch):
    """显式指路时不再往下猜：候选链只剩覆盖项，且服务其字节。"""
    manifest = tmp_path / "only.json"
    manifest.write_text('{"line":"probe"}')
    monkeypatch.setenv(wellknown.ENV_MANIFEST_PATH, str(manifest))

    assert wellknown.manifest_candidates() == [manifest]
    status, body, _ = wellknown.manifest_response()
    assert status == 200
    assert body == manifest.read_bytes()


def test_missing_manifest_is_404_not_500(tmp_path, monkeypatch):
    monkeypatch.setenv(wellknown.ENV_MANIFEST_PATH, str(tmp_path / "nope.json"))
    status, body, headers = wellknown.manifest_response()
    assert status == 404
    assert b"not published" in body
    assert "cache-control" not in headers


def test_invalid_json_is_500_not_404(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setenv(wellknown.ENV_MANIFEST_PATH, str(bad))
    status, body, headers = wellknown.manifest_response()
    assert status == 500
    assert b"not valid JSON" in body
    assert "cache-control" not in headers


def test_candidate_chain_finds_repo_manifest_from_any_cwd(tmp_path, monkeypatch):
    """镜像里包在 site-packages、CWD 才是仓根——链必须至少命中源码树那份。"""
    monkeypatch.delenv(wellknown.ENV_MANIFEST_PATH, raising=False)
    monkeypatch.chdir(tmp_path)
    assert wellknown.manifest_path() == REPO_MANIFEST


def test_real_assembly_serves_the_manifest():
    """真装配：模块级 mcp 已挂路由，http_app() 上请求该路径 200 且字节相同。"""
    pytest.importorskip("fastmcp")
    from starlette.testclient import TestClient

    from fd_find_data_business_mcp.server import mcp

    client = TestClient(mcp.http_app())

    res = client.get(wellknown.WELL_KNOWN_PATH)
    assert res.status_code == 200, res.text
    assert res.content == REPO_MANIFEST.read_bytes()
    assert res.headers["content-type"] == "application/json; charset=utf-8"
    assert res.headers["cache-control"] == "public, max-age=300"
