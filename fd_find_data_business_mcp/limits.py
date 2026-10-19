"""返回体积上限与续读约定（mcp-result-contract）。

**本文件是 fd-legal-search-mcp 同名模块的逐字镜像**：两个服务各自是独立
git 仓、独立发布，跨仓共享代码需要发布一个 PyPI 包（fd-quant 无版本控制
时踩过的坑），故采用"逐字镜像 + 一致性检查"的既有做法。改动时两边必须
同步；`scripts/check_limits_mirror.py` 会比对差异并失败。

原文说明如下。


**为什么集中在这里**：上限此前散落在四个工具模块里（`_MAX_LIMIT` 在三个文件
各写一份、`_MAX_CONTENT_CHARS` 与 `_MAX_WINDOW_CHARS` 各管一段），改一处忘一处
的结果是"同一个服务里两个工具对同一个概念用不同的上限"。本模块是唯一真源，
工具模块只引用、不再定义。

**为什么按字符而不是字节**：Python 的 `str` 切片天然落在码点边界，中文与
emoji（含星平面字符）都不会被切成半个字符。按字节切则必须在边界回退，回退
逻辑一旦写错就是乱码——而乱码在拼接后无法自愈。故全链路以字符为单位，
`test_limits.py` 用中文 + emoji 实证拼接后与原文逐字符相等。

**统一的续读约定**（三个正文工具共用，调用方不必逐个记键名）：

- `content` / `chunks` —— 本次返回的片段
- `content_offset` / `chunk_offset` —— 本段的起始位置
- `total_chars` / `total_chunks` —— 原始规模（**始终给出**，不只在上限时给）
- `content_truncated` / `truncated` —— 是否还有后续
- `next_offset` —— 续读位置（仅在还有后续时出现）
- `note` —— 人读的续读说明（含具体 offset 数值）

约定里刻意**不**提供"一次返回全文"的开关：spec 要求"显式要求全文时同样受上限
约束"，所以取全的唯一方式是循环续读。少一个开关，就少一条绕过上限的路径。
"""
from __future__ import annotations

from typing import Any

# ── 上限（唯一真源）───────────────────────────────────────────────

#: 单个正文类字段一次最多返回的字符数（默认窗口）。12000 字符约合中文 8k token，
#: 留给推理与工具编排的空间仍然充足；实测 law_db.legal_documents 有 493 篇正文
#: 超过这个值、最长 177,139 字符——不分段就会一次吃掉整个上下文预算。
DEFAULT_BODY_CHARS = 12_000

#: 调用方可显式放大到的上限。再大就不是"一次调用能消费的结果"，而是分页场景。
MAX_BODY_CHARS = 40_000

#: 检索类工具单次返回的条目数上限（与调用方传的 limit 取小）。
MAX_LIST_ITEMS = 200

#: 语义检索的 top_k 上限。
MAX_TOP_K = 50

#: 法规条文窗口：默认与上限（按"条"计，不按字符）。
DEFAULT_CHUNK_WINDOW = 200
MAX_CHUNK_WINDOW = 500

#: 条级引用解析里单条摘录的字符上限（引用场景只需要上下文，不需要全文）。
MAX_EXCERPT_CHARS = 2_000


def clamp(value: Any, *, low: int, high: int) -> int:
    """把调用方给的数值夹到 [low, high]；非数值或 None 取 low。

    **静默夹取而不是报错**：上限是服务的自我保护，不是入参校验。调用方传
    limit=10000 的意思是"尽量多给"，回 200 条并带 truncated=true 是诚实的
    回应；报错反而让"想多要一点"变成失败。
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(n, high))


def slice_window(text: str, offset: int = 0, limit: int = DEFAULT_BODY_CHARS) -> tuple[str, int, bool]:
    """按字符切一段正文，返回 (片段, 下一段起点, 是否还有后续)。

    字符切分保证多字节安全：中文与 emoji 都不会被劈开。`offset` 越界时返回
    空片段且 `has_more=False`——**越界由调用方决定如何回报**（本服务的三个
    正文工具都选择报 not_found 并点名实际长度，而不是回一个空串让人误以为
    文档到此为止）。
    """
    start = max(0, int(offset))
    size = clamp(limit, low=1, high=MAX_BODY_CHARS)
    chunk = text[start:start + size]
    end = start + len(chunk)
    return chunk, end, end < len(text)


def truncation_fields(
    *,
    total: int,
    offset: int,
    returned: int,
    unit: str = "字符",
    truncated_key: str = "content_truncated",
    offset_key: str = "content_offset",
    total_key: str = "total_chars",
) -> dict[str, Any]:
    """拼出统一续读字段。`unit` 只影响 note 的措辞（字符 / 条）。

    键名可覆盖是因为法规工具按"条"分页（`chunk_offset` / `total_chunks`），
    正文工具按"字符"分段；**语义相同、单位不同**，强行统一键名会让其中一个
    的名字撒谎。
    """
    end = offset + returned
    out: dict[str, Any] = {
        offset_key: offset,
        total_key: total,
        truncated_key: end < total,
    }
    if end < total:
        out["next_offset"] = end
        out["note"] = (
            f"本次返回 [{offset}, {end}) 共 {returned} {unit}，原始规模 {total} {unit}；"
            f"续读请传 offset={end}。"
        )
    return out
