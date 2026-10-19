#!/usr/bin/env python3
"""limits.py 跨仓一致性检查（mcp-result-contract task 3.1）。

`fd-legal-search-mcp` 与 `fd-find-data-business-mcp` 各自是独立 git 仓、独立
发布，跨仓共享代码需要发布一个 PyPI 包——而 fd-quant 没有版本控制时踩过的坑
说明那条路当下走不通。故采用本仓既有的"逐字镜像 + 一致性检查"做法：

- 两个 `limits.py` 的**代码部分**必须逐字相同（上限常量与切分/续读语义）；
- 只允许模块 docstring 有差异（business-mcp 那份多一段"这是镜像"的说明）。

**为什么必须检查**：两份上限一旦分叉，同一个"默认窗口"在两个服务里就是两个
数字，而两边各自的测试都会绿——只有同时读两份代码的人才发现。

用法：
    python3 scripts/check_limits_mirror.py
退出码：0 一致 / 1 分叉（列出首个差异行）。
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MIRROR = REPO / "fd_find_data_business_mcp" / "limits.py"
SOURCE = REPO.parent / "fd-legal-search-mcp" / "fd_legal_search_mcp" / "limits.py"


def code_part(path: Path) -> str:
    """去掉模块 docstring，只留可执行部分。

    docstring 允许不同（镜像方要说明自己是镜像），代码必须逐字相同。
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith('"""'):
        return text
    end = text.find('"""', 3)
    return text[end + 3:].lstrip("\n") if end != -1 else text


def main() -> int:
    if not SOURCE.exists():
        print(f"· 源文件不在本工作区（{SOURCE}），跳过镜像检查", file=sys.stderr)
        return 0
    a, b = code_part(SOURCE), code_part(MIRROR)
    if a == b:
        print(f"✓ limits.py 镜像一致（{len(a)} 字符代码，docstring 允许不同）")
        return 0
    al, bl = a.splitlines(), b.splitlines()
    for i, (x, y) in enumerate(zip(al, bl), 1):
        if x != y:
            print(f"✗ limits.py 分叉（第 {i} 行）：", file=sys.stderr)
            print(f"    legal-search : {x!r}", file=sys.stderr)
            print(f"    business     : {y!r}", file=sys.stderr)
            break
    else:
        print(f"✗ limits.py 行数不同：legal-search {len(al)} 行 / business {len(bl)} 行",
              file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
