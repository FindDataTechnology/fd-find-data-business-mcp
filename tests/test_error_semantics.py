"""错误语义契约（mcp-error-semantics 组 4.5：不可静默降级为成功）。

本服务（32 工具）的错误形状是**结构化降级载荷**而不是异常：

    {"status": "domain_unavailable", "detail": "<ExceptionClass>"}

规范允许这条路径（"返回错误或显式降级标记"），前提是**降级必须可辨认**。
本文件钉住三件事：

1. 域库不可用时，工具返回的载荷带 `domain_unavailable` 状态，而不是空列表；
2. `detail` 只含异常类名——不带 host / SQL / 凭据（泄露面控制）；
3. 未配置时也是同一个状态，与"连不上"可区分（detail 不同）。

**为什么这里不用 isError**：本服务的消费方（pack 技能）按 `status` 分支，
改成 isError 会让它们的既有分支全部失效。而 `domain_unavailable` 本身已经
满足契约要求——它明确说"取不到"，不会被误读成"真的没有"。
"""
from __future__ import annotations

import pytest

from fd_find_data_business_mcp import domain


def test_unavailable_payload_is_recognizable():
    out = domain._unavailable("OperationalError")
    assert out["status"] == domain.UNAVAILABLE
    assert out["detail"] == "OperationalError"


def test_unavailable_payload_carries_no_credentials_or_host():
    """detail 只放异常类名：消息里可能含 host、SQL 片段或解码失败的凭据。"""
    out = domain._unavailable("OperationalError")
    text = str(out).lower()
    for leak in ("password", "postgres://", "postgresql://", "@", "select "):
        assert leak not in text, f"降级载荷泄露了 {leak!r}"


def test_not_configured_is_distinguishable_from_unreachable(monkeypatch):
    """"没配"与"连不上"是两个不同的 detail——运维处置不同。"""
    monkeypatch.setattr(domain, "configured", lambda: False)
    out = domain.query("law_db", "SELECT 1")
    assert out["status"] == domain.UNAVAILABLE
    assert out["detail"] == "not_configured"


def test_connection_failure_yields_unavailable_not_empty_list(monkeypatch):
    """连接失败必须返回降级标记，不能返回空列表。

    空列表 = "这个域没有数据"；降级标记 = "这个域取不到"。前者会让调用方
    以为查询结果为空并据此作答。
    """
    monkeypatch.setattr(domain, "configured", lambda: True)

    class BoomEngine:
        def connect(self):
            raise RuntimeError("connection refused")

    monkeypatch.setattr(domain, "get_engine", lambda name: BoomEngine())
    out = domain.query("law_db", "SELECT 1")
    assert isinstance(out, dict), "失败不能返回列表"
    assert out["status"] == domain.UNAVAILABLE
    assert out["detail"] == "RuntimeError"


def test_successful_query_returns_rows_not_a_status_dict(monkeypatch):
    monkeypatch.setattr(domain, "configured", lambda: True)

    class FakeMappings:
        def all(self):
            return [{"a": 1}]

    class FakeConn:
        def execute(self, *a, **k):
            return type("R", (), {"mappings": lambda self: FakeMappings()})()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakeEngine:
        def connect(self):
            return FakeConn()

    monkeypatch.setattr(domain, "get_engine", lambda name: FakeEngine())
    out = domain.query("law_db", "SELECT 1")
    assert out == [{"a": 1}]
