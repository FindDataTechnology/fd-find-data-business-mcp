"""线自描述端点 ``/.well-known/line-manifest.json``（change converge-line-identity-and-keys 1.4）。

家族实现：公开、免鉴权、零网络。**原样回吐**本仓 ``line-manifest.json``——仓内那份是
唯一真源，这个端点与注册表的 ``metadata.identity`` 都只是它的投影；这里一旦加工，就
出现第二个真源，两处投影必然漂移（spec 1.4 的「两个投影都与源逐字段一致」正是为此）。

为什么路径是「候选链」而不是一条固定的：镜像是 ``pip install .`` + ``python -m`` 起的，
包的 ``__file__`` 落在 site-packages，``parents[1]`` 是 site-packages 而不是仓根；而
``WORKDIR /app`` 下那份 ``line-manifest.json``（Dockerfile ``COPY line-manifest.json ./``）
才是镜像里真正的那份。源码树 → CWD → 包同级依次找，任一命中即可；``LINE_MANIFEST_PATH``
一项即封顶（部署方显式指路时不再猜）。

鉴权面：本线的门是 ``FastMCP(..., auth=_build_auth())`` 的 Logto JWT 校验，它由 fastmcp
只挂在 ``/mcp`` 路由上；``custom_route`` 注册的路由天然不经过它，所以这里**不需要**
``EXEMPT_PATHS``。若将来在本仓加一个包住整个 ASGI app 的共享 bearer 门（像
``fd-text-core`` / ``fd-info`` 的 ``BearerAuthMiddleware``），必须同时把
``WELL_KNOWN_PATH`` 加进那份门的豁免，否则匿名发现请求会被 401 挡住。

两个状态码分得很清：**缺失=404**（该线未发布清单，属配置事实）；**存在但不可解析=500**
（属缺陷，不能伪装成 404 让调用方以为"本来就没有"）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

#: 与 ``fd-info`` / ``fd-text-core`` / ``fd-legal-search-mcp`` 逐字相同的路径常量。
WELL_KNOWN_PATH = "/.well-known/line-manifest.json"
ENV_MANIFEST_PATH = "LINE_MANIFEST_PATH"
#: 清单是运行期契约：改一次要生效，但不能让边缘缓存长期撒谎。
CACHE_CONTROL = "public, max-age=300"

_CONTENT_TYPE = "application/json; charset=utf-8"


def manifest_candidates() -> list[Path]:
    """按优先级列出可能放清单的位置（``LINE_MANIFEST_PATH`` 一项即封顶）。"""
    override = os.environ.get(ENV_MANIFEST_PATH)
    if override:
        return [Path(override)]
    here = Path(__file__).resolve()
    return [
        here.parents[1] / "line-manifest.json",  # 源码树（parents[1] 是仓根）
        Path.cwd() / "line-manifest.json",       # 镜像：WORKDIR /app 才是仓根
        here.parent / "line-manifest.json",      # 清单与包同级（site-packages 兜底）
    ]


def manifest_path() -> Path:
    """实际取用的清单路径；都不存在时返回首选位置（让 404 能点名期望路径）。"""
    candidates = manifest_candidates()
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def manifest_response() -> tuple[int, bytes, dict[str, str]]:
    """(status, body, headers) —— 不读网络、不建会话。

    成功时附 ``Cache-Control``，失败时不附：让缓存别把一个"暂时没发布"的 404 记成
    长期事实。
    """
    path = manifest_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return 404, b'{"detail":"line-manifest not published"}', {}
    except OSError as exc:
        detail = f"line-manifest unreadable: {type(exc).__name__}"
        return 500, json.dumps({"detail": detail}).encode(), {}
    try:
        json.loads(raw)
    except ValueError:
        return 500, b'{"detail":"line-manifest is not valid JSON"}', {}
    return 200, raw, {"cache-control": CACHE_CONTROL}


def register(mcp) -> None:
    """把端点挂到 ``FastMCP`` 实例上（stdio 形态下无 HTTP 面，不挂）。

    用 ``custom_route`` 而不是自己包一层 ASGI：路由属于应用本身，于是**两个分支都在**
    ——配了 ``FDBIZ_JWKS_URI`` 时走 ``http_app()``、没配时走 ``run()``，同一份代码。
    fastmcp 只对 ``/mcp`` 路由套 ``RequireAuthMiddleware``，所以这条路由公开可达；
    仓库里没有第二道 HTTP 门，也就没有豁免表要同步。
    """
    if not hasattr(mcp, "custom_route"):  # pragma: no cover - 依赖缺失时宁可起不来
        raise RuntimeError(
            "fastmcp 缺 custom_route：无法挂 " + WELL_KNOWN_PATH + "（升级依赖，别静默跳过）"
        )
    from starlette.responses import Response

    @mcp.custom_route(WELL_KNOWN_PATH, methods=["GET"])
    async def line_manifest(_request):  # noqa: ANN001 - ASGI 边界，不需要类型
        status, body, headers = manifest_response()
        return Response(
            content=body, status_code=status, media_type=_CONTENT_TYPE, headers=headers
        )
