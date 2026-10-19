"""法律域四组同名工具的溯源三件套（finance-legal-value-realization 组 5，task 5.3）。

**为什么要有这个模块**：同一批检索能力在两个服务入口各有一份实现——lex 侧
（`fd-legal-search-mcp`）的每个结果都带 `provenance` 三件套
（`source` / `stable_id` / `as_of`，由 `ensure_provenance` 强制），而本侧此前
只返回业务字段。调用方因此必须按入口分支解析——这正是 spec `lex-mcp-surface`
「两侧同名工具返回的溯源字段三件套 SHALL 形状一致」要禁掉的形态。

本模块让**本侧**也产出同形状的三件套，字段名与语义与 lex 侧逐项对齐：

- `source`：数据出处标识（库.表），与 lex 侧同域同值。
- `stable_id`：条目在出处内的稳定标识（字符串），可回查。
- `as_of`：该条目数据口径的 ISO 日期（取入库时间的日期部分）。

**这不是把本侧变成代理**：本侧仍直连联邦库（保持其现有可用性与降级语义），
只补上溯源字段形状。退役仍由 `lex-private-distribution-full` 承载（design.md D7）。

**`as_of` 取不到时怎么办**：空串，并由 `ensure_provenance` 抛错——缺口径是 bug，
不是可降级项（与 lex 侧同一纪律）。宁可让调用失败，也不让"数据是哪一天的"变成
一个猜不出的问题。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: 四组工具的 `source` 取值（与 lex 侧 `fd_legal_search_mcp` 的常量同值）。
SOURCE_LAWS = "law_db.laws"
SOURCE_CHUNKS = "law_db.law_chunks"
SOURCE_WENSHU = "wenshu_db.judgments"
SOURCE_GUIDE_CASES = "law_db.guide_cases"
SOURCE_LEGAL_DOCS = "law_db.legal_documents"


@dataclass(frozen=True)
class Provenance:
    """溯源三件套。`stable_id` 一律是字符串——整数 id 与文本 id 都可能出现。"""

    source: str
    stable_id: str
    as_of: str  # ISO-8601 数据口径日期

    def as_dict(self) -> dict[str, str]:
        return {"source": self.source, "stable_id": self.stable_id, "as_of": self.as_of}


def as_of_date(created_at: Any) -> str:
    """行级 `as_of`：入库时间的日期部分。与 lex 侧 `_as_of_date` 同实现。"""
    if isinstance(created_at, datetime):
        return created_at.date().isoformat()
    if isinstance(created_at, str) and len(created_at) >= 10:
        return created_at[:10]
    return ""


def with_provenance(item: dict[str, Any], prov: Provenance) -> dict[str, Any]:
    """给一条结果挂上 `provenance`（原地修改并返回，与 lex 侧同调用形态）。"""
    item["provenance"] = prov.as_dict()
    return item


def ensure_provenance(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """契约守卫：任一条目缺三件套即抛 AssertionError（与 lex 侧同纪律）。"""
    for i, item in enumerate(items):
        prov = item.get("provenance") if isinstance(item, dict) else None
        if not (isinstance(prov, dict) and prov.get("source")
                and prov.get("stable_id") and prov.get("as_of")):
            raise AssertionError(f"item[{i}] 缺 provenance 三件套: {str(item)[:120]}")
    return items
