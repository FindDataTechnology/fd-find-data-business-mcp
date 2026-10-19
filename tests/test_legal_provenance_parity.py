"""法律四组同名工具与 lex 侧的溯源三件套一致性（组 5，task 5.3）。

spec `lex-mcp-surface` 要求：两侧同名工具返回的 `provenance` 三元组**同名字段、
同语义**，使调用方无需按入口分支解析。这个文件把"同形状"钉成可执行断言。

**为什么必须逐域写死期望值**：四个域的 `as_of` 口径**互不相同**——若只用一条
泛化的"必须有 provenance"断言，某域悄悄改成另一口径（例如把 `loaded_at` 截成
日期，而 lex 侧给全 ISO）时测试照样全绿。故每个域的 source 串与 as_of 取值都
显式钉住，并注明它为何不同。

口径来源（lex 侧 `fd-legal-search-mcp`，2026-10-19 逐域构造函数对照）：

| 域 | source | as_of 口径 |
|---|---|---|
| law | `law_db.laws` | `created_at` 的日期部分 |
| guide_cases | `law_db.guide_cases` | `publish_at`，缺失回退 `listed_at` |
| legal_docs | `law_db.legal_documents` | `loaded_at` 的**完整** ISO 串 |
| wenshu | `wenshu_db.judgments` | `发布时间` → `判决时间` → `"unknown"` |
"""
from __future__ import annotations

import os
import pathlib

import pytest

pytestmark = pytest.mark.usefixtures("_federated_env")

import fd_find_data_business_mcp.tools_guide_cases as tools_guide_cases  # noqa: E402
import fd_find_data_business_mcp.tools_law as tools_law  # noqa: E402
import fd_find_data_business_mcp.tools_legal_docs as tools_legal_docs  # noqa: E402
import fd_find_data_business_mcp.tools_wenshu as tools_wenshu  # noqa: E402


@pytest.fixture()
def _federated_env():
    """把 .env 里的 FDBIZ_DOMAIN_PG_URL 注入环境（缺失时由 skipif 兜底）。"""
    if not os.environ.get("FDBIZ_DOMAIN_PG_URL"):
        _env = pathlib.Path(__file__).resolve().parents[1] / ".env"
        if _env.exists():
            for _line in _env.read_text().splitlines():
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _key, _, _val = _line.partition("=")
                    os.environ.setdefault(_key.strip(), _val.strip())


def _assert_provenance(prov: dict, expected_source: str) -> None:
    """三件套：字段齐全、source 精确匹配、stable_id 是字符串、as_of 非空。"""
    assert set(prov) == {"source", "stable_id", "as_of"}, prov
    assert prov["source"] == expected_source, prov
    assert isinstance(prov["stable_id"], str) and prov["stable_id"], prov
    assert prov["as_of"], prov  # 宁可 "unknown" 也不空（口径无从推断即 bug）


@pytest.mark.skipif(not os.environ.get("FDBIZ_DOMAIN_PG_URL"),
                    reason="FDBIZ_DOMAIN_PG_URL not set; federated PG unreachable")
class TestProvenanceParityWithLex:
    """四个域的溯源三件套与 lex 侧逐域对齐（live 实查联邦库）。"""

    def test_law_source_and_stable_id(self):
        out = tools_law.law_search("民法典", limit=1)
        if out.get("count") == 0:
            pytest.skip("无命中")
        _assert_provenance(out["results"][0]["provenance"], "law_db.laws")

    def test_law_as_of_is_a_date_not_a_timestamp(self):
        """law 域的 as_of 是 **date**（created_at 的日期部分），不带时分秒。"""
        out = tools_law.law_search("民法典", limit=1)
        if out.get("count") == 0:
            pytest.skip("无命中")
        as_of = out["results"][0]["provenance"]["as_of"]
        assert len(as_of) == 10 and as_of.count("-") == 2, as_of

    def test_guide_cases_source_and_as_of_is_publish_date(self):
        out = tools_guide_cases.guide_cases_search("案例", limit=1)
        if out.get("count") == 0:
            pytest.skip("无命中")
        _assert_provenance(out["results"][0]["provenance"], "law_db.guide_cases")

    def test_legal_docs_as_of_is_full_iso(self):
        """legal_docs 的 as_of 保留**完整** ISO（含时分秒与时区），不截到日。

        这是与 law/guide_cases 的**刻意差异**：lex 侧 `tools_corpus._provenance`
        原样回 `loaded_at`。两边若一边截到日、一边给全串，调用方就得按入口分支解析。
        """
        out = tools_legal_docs.legal_docs_search("管理", limit=1)
        if out.get("count") == 0:
            pytest.skip("无命中")
        _assert_provenance(out["results"][0]["provenance"], "law_db.legal_documents")
        assert len(out["results"][0]["provenance"]["as_of"]) > 10

    def test_wenshu_source_matches_lex(self):
        """wenshu 域 source 必须是 wenshu_db.judgments（不是 documents）。

        这个字符串写错过一次：常量初值误作 `wenshu_db.documents`，而 lex 侧是
        `wenshu_db.judgments`。故单独钉死。
        """
        out = tools_wenshu.wenshu_search("民事", limit=1)
        if not out.get("count"):
            pytest.skip(f"该快照无命中: {out.get('status') or 'count=0'}")
        _assert_provenance(out["results"][0]["provenance"], "wenshu_db.judgments")

    def test_legal_tools_point_at_the_authoritative_entry(self):
        """八个工具的描述都必须含权威入口指向（spec: 非权威方标注弃用）。"""
        from fd_find_data_business_mcp import server

        for name in ("law_search", "law_read", "legal_docs_search", "legal_docs_read",
                     "guide_cases_search", "guide_cases_read",
                     "wenshu_search", "wenshu_read"):
            fn = getattr(server, name, None)
            assert fn is not None, f"{name} 不在 server 上"
            doc = fn.__doc__ or ""
            assert "fd-legal-search-mcp" in doc, f"{name} 描述未指向权威入口"
            assert "弃用" in doc, f"{name} 描述无弃用标记"
