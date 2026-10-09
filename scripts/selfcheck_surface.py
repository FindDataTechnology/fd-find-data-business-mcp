#!/usr/bin/env python3
"""tool-surface manifest 双向 selfcheck（mcp-surface-contract）。

断言两个方向：
  1. 一致：manifest 声明的工具 == 代码注册的工具（新增/删除工具而 manifest
     未同步即失败，反向同理）
  2. 闭合：消费侧必需依赖（data_binding / mcp_servers 声明行）中的本 server
     工具 ⊆ manifest 的 commercial 集

代码注册面优先 `mcp.list_tools()` 运行时真值，不可导入时回退源码码点。

用法：
    python3 scripts/selfcheck_surface.py
    python3 scripts/selfcheck_surface.py --consumers /path/to/suites /path/to/skills
退出码：0 通过 / 1 失败（列出差异）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "tool-surface.json"
SERVER_NAME = "fd-find-data-business-mcp"

TOOL_PATTERN = re.compile(rf"mcp__{SERVER_NAME}__([A-Za-z0-9_.\-]+)")


def fail(problems: list[str]) -> "None":
    print(f"✗ {len(problems)} 项不通过：", file=sys.stderr)
    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    raise SystemExit(1)


def load_manifest() -> dict:
    if not MANIFEST.exists():
        fail([f"manifest 缺失：{MANIFEST}（契约：缺失即未上牌）"])
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def manifest_tools(m: dict) -> dict[str, str]:
    out = {}
    for g in m.get("tool_groups", []):
        level = g.get("level")
        if level not in ("commercial", "internal"):
            fail([f"tool_groups.{g.get('group')} 的 level 非法：{level!r}"])
        for t in g.get("tools", []):
            if t in out and out[t] != level:
                fail([f"工具 {t} 在多个 group 中级别冲突"])
            out[t] = level
    return out


async def runtime_tools() -> set[str]:
    sys.path.insert(0, str(REPO))
    try:
        from fd_find_data_business_mcp.server import mcp  # type: ignore

        tools = await mcp.list_tools()
        names = {getattr(t, "name", None) or t.get("name") for t in tools}
        return {n for n in names if n}
    except Exception:
        return source_tools()


def source_tools() -> set[str]:
    names: set[str] = set()
    for f in (REPO / "fd_find_data_business_mcp").glob("*.py"):
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(
            r"@mcp\.tool\s*(?:\([^)]*\))?\s*\n\s*(?:async\s+)?def\s+([A-Za-z0-9_]+)", text
        ):
            names.add(m.group(1))
    return names


def consumer_tools(consumers: list[Path]) -> dict[str, set[str]]:
    """消费侧**必需依赖**中点名的本 server 工具（data_binding/mcp_servers 行；
    optional_binding 为可选增强，不受闭合约束）。"""
    out: dict[str, set[str]] = {}
    for c in consumers:
        if not c.exists():
            fail([f"消费侧路径不存在：{c}"])
        used: set[str] = set()
        for f in list(c.rglob("*.json")) + list(c.rglob("*.md")):
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for line in text.splitlines():
                if re.match(r"\s*(data_binding|mcp_servers)\s*:", line):
                    used |= set(TOOL_PATTERN.findall(line))
        if used:
            out[str(c)] = used
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--consumers", nargs="*", type=Path, default=[])
    args = ap.parse_args()

    m = load_manifest()
    declared = manifest_tools(m)
    actual = await runtime_tools()
    problems: list[str] = []

    missing = sorted(actual - set(declared))
    stale = sorted(set(declared) - actual)
    if missing:
        problems.append(f"[注册表漂移] 代码已注册但 manifest 未声明：{', '.join(missing)}")
    if stale:
        problems.append(f"[manifest 陈旧] manifest 声明但代码未注册：{', '.join(stale)}")

    expected = m.get("registry", {}).get("expected_tool_count")
    if expected is not None and len(actual) != expected:
        problems.append(f"[工具数不符] 运行时 {len(actual)} ≠ manifest 声明 {expected}")

    internal = {t for t, lv in declared.items() if lv == "internal"}
    if args.consumers:
        for where, used in consumer_tools(args.consumers).items():
            over = sorted(used & internal)
            if over:
                problems.append(f"[消费侧越界] {where} 必需依赖点名了 internal 工具：{', '.join(over)}")

    commercial = sorted(t for t, lv in declared.items() if lv == "commercial")
    print(f"manifest：{len(declared)} 工具（commercial {len(commercial)} / internal {len(internal)}）")
    print(f"运行时：{len(actual)} 工具")
    if problems:
        fail(problems)
    print("✓ selfcheck 双向断言通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
