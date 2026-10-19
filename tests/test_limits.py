"""返回体积上限与续读（mcp-result-contract 组 3）。

business-mcp 的三个正文读取工具（legal_docs_read / guide_cases_read /
wenshu_read）此前**完全无上限**：与 fd-legal-search-mcp 读的是同一批语料，
那边最长正文 177,139 字符——不分段就会一次吃掉调用方的整个上下文预算。

本文件钉住三件事：
1. 超限必截断且给出续读位置；
2. 分段拼接与原文**逐字符相等**（含中文与 emoji 的多字节安全）；
3. 上限定义与 fd-legal-search-mcp 的同名模块逐字一致（镜像检查）。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

from fd_find_data_business_mcp import limits

REPO = pathlib.Path(__file__).resolve().parents[1]


def test_slice_window_basic():
    chunk, nxt, more = limits.slice_window("abcdefghij", 0, 4)
    assert (chunk, nxt, more) == ("abcd", 4, True)


def test_slice_window_exact_multiple_is_not_end_of_page():
    """长度恰为窗口整数倍时最后一段仍是满长度——不能靠"短页"判结束。"""
    _, nxt, more = limits.slice_window("a" * 8, 4, 4)
    assert (nxt, more) == (8, False)


@pytest.mark.parametrize("limit", [1, 2, 3, 7, 100, 1000])
def test_multibyte_split_reassembles_exactly(limit):
    text = "《中华人民共和国民法典》第一编总则😀第1条🧑‍⚖️规定：民事主体的人身权利受法律保护。"
    parts, offset = [], 0
    while True:
        chunk, offset, more = limits.slice_window(text, offset, limit)
        parts.append(chunk)
        if not more:
            break
    assert "".join(parts) == text
    assert all(p.encode("utf-8").decode("utf-8") == p for p in parts)


def test_truncation_fields_always_carry_total():
    f = limits.truncation_fields(total=100, offset=0, returned=100)
    assert f["total_chars"] == 100 and f["content_truncated"] is False
    assert "next_offset" not in f

    f2 = limits.truncation_fields(total=100, offset=0, returned=40)
    assert f2["content_truncated"] is True and f2["next_offset"] == 40


def test_clamp_does_not_raise_on_junk():
    assert limits.clamp("abc", low=1, high=10) == 1
    assert limits.clamp(99999, low=1, high=10) == 10


def test_limits_module_mirrors_the_legal_search_copy():
    """两个服务的 limits.py 代码部分必须逐字相同。

    上限一旦分叉，同一个"默认窗口"在两个服务里就是两个数字，而两边各自的
    测试都会绿——只有同时读两份代码的人才会发现。
    """
    r = subprocess.run([sys.executable, str(REPO / "scripts" / "check_limits_mirror.py")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or r.stdout


def test_wenshu_read_pages_and_reassembles(monkeypatch):
    """wenshu_read 真实分页：超限截断、可续读、拼接等于原文。"""
    import asyncio

    from fastmcp import Client

    from fd_find_data_business_mcp import tools_wenshu as tw
    from fd_find_data_business_mcp.server import mcp

    body = "本院认为：被告应于本判决生效之日起十日内支付原告货款。" * 500
    row = {"doc_id": "d1", "案号": "（2024）测1号", "标题": "测试判决",
           "法院": "某法院", "案由": "买卖合同", "省": "某省", "市": "某市",
           "案件类型": "民事", "文书类型": "判决书", "审理程序": "一审",
           "法院级别": "基层", "判决年份": 2024, "判决结果": "支持",
           "判决时间": "2024-01-01", "发布时间": "2024-01-02",
           "受理时间": "2023-12-01", "body_shape": "html", "block_key": "k1"}

    monkeypatch.setattr(tw, "query", lambda domain, sql, params: [row])
    monkeypatch.setattr(tw.wenshu_body, "fetch_judgment_body",
                        lambda key, did: body)

    async def run():
        parts, offset, truncated_once = [], 0, False
        async with Client(mcp) as client:
            while True:
                res = await client.call_tool(
                    "wenshu_read", {"doc_id": "d1", "offset": offset, "max_chars": 1000})
                d = res.data
                assert d["total_chars"] == len(body)
                parts.append(d["content"])
                if d["content_truncated"]:
                    truncated_once = True
                    offset = d["next_offset"]
                else:
                    break
        return parts, truncated_once

    parts, truncated_once = asyncio.run(run())
    assert truncated_once, "超长正文应至少截断一次"
    assert "".join(parts) == body
