"""Unified indicator-series router — one entry, registry identities.

``indicator_series_read`` (server.py) accepts unified-registry identities
(``source_db:native_code``), splits them here, and dispatches per store to
the existing read paths. The router table is explicit (same convention as
the domain tools' adapter organization): one entry per supported source_db;
everything else is an explicit "not supported" rejection, never a silent
empty set (truthful-miss discipline). Routers return the canonical row
shape ``{indicator_id, date, value, unit, brand: "finddata"}`` — ``date``
is ISO ``YYYY-MM-DD`` (annual stores get their year-end date);
``indicator_id`` echoes the caller's identity so batch callers can line
rows up. A valid identity whose range simply has no data is a truthful
empty row list — only an invalid identity or unreachable store is an error.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable

#: Entity dictionary per source (bounded; mirrored in the tool docstring).
ENTITY_DICTIONARY: dict[str, str] = {
    "fd_open_data": (
        "entity_type ∈ {country, city, stock, fund, bond, index, future, "
        "crypto, organization, industry}（须与概念自身维度一致）；"
        "entity_id = 整型实体 id"
    ),
    "yearbook_catalog": (
        "entity_type 固定 region；entity_id = region 名（如 北京市 / 全国）；"
        "省级下钻（province 参数）后续版本接入"
    ),
    "world_bank": (
        "entity_type 固定 country；entity_id = ISO3 码或中文名（CHN / 中国）"
    ),
}


def split_indicator_id(indicator_id: str) -> tuple[str, str]:
    """Split ``source_db:native_code`` — one colon, both sides non-empty."""
    text = (indicator_id or "").strip()
    sdb, sep, code = text.partition(":")
    if not sep or not sdb.strip() or not code.strip() or ":" in code:
        raise SeriesRouteError(f"indicator 标识不合法: {indicator_id!r}")
    return sdb.strip(), code.strip()


class SeriesRouteError(ValueError):
    """One identity's rejection. Message is frozen contract (wire fixture)."""


def _num(v: Any) -> Any:
    if v is None or isinstance(v, (int, float)):
        return v
    if isinstance(v, Decimal):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def _year_to_range(start: str | None, end: str | None) -> tuple[int | None, int | None]:
    """Annual stores take the year part of ISO bounds (None passes through)."""
    return (int(start[:4]) if start else None, int(end[:4]) if end else None)


def _date_of_year(year: int) -> str:
    return f"{year:04d}-12-31"


# --- semantic core (fd_open_data mirror: active concepts) --------------------

def _semantic_router(native_code: str, entity_type: str, entity_id: int,
                     start: str, end: str) -> list[dict]:
    from fd_open_data_mcp.db import get_database
    from fd_open_data_mcp.fetch.cache import read_cache_range
    from fd_open_data_mcp.fetch.dispatch import read_range as _read_range
    from fd_open_data_mcp.models import Concept

    s = get_database().get_session()
    try:
        concept = (
            s.query(Concept)
            .filter(
                Concept.code == native_code,
                Concept.entity_type == entity_type,
                Concept.deprecated.is_(False),
            )
            .first()
        )
        if concept is None:
            raise SeriesRouteError(f"indicator 解析失败: fd_open_data:{native_code}")
        cached = read_cache_range(s, concept.id, entity_type, entity_id, start, end)
        if cached:
            rows = [{"date": r.date, "value": _num(r.value), "unit": r.unit}
                    for r in cached]
        else:
            # Cold range -> one ranked range fetch, bulk-write, return
            # (same discipline as read_range).
            fetched = _read_range(s, [concept.id], entity_type, entity_id, start, end)
            rows = [{"date": r["date"], "value": _num(r["value"]), "unit": r["unit"]}
                    for r in fetched.get(concept.id, [])]
        # One row per date: the dedup view may be absent on a rolling-window
        # mirror, and the base-table fallback can serve the same point from
        # two sources. Consumers key datasets on date — keep the first
        # (highest-ranked) occurrence.
        seen: set[str] = set()
        deduped = []
        for r in rows:
            if r["date"] in seen:
                continue
            seen.add(r["date"])
            deduped.append(r)
        rows = deduped
        iid = f"fd_open_data:{native_code}"
        return [{"indicator_id": iid, "date": r["date"], "value": r["value"],
                 "unit": r["unit"], "brand": "finddata"} for r in rows]
    finally:
        s.close()


# --- yearbook (annual; region is the entity) ---------------------------------

def _yearbook_router(native_code: str, entity_type: str, entity_id: str,
                     start: str, end: str) -> list[dict]:
    from fd_find_data_business_mcp.tools_yearbook import yearbook_read

    if entity_type != "region":
        raise SeriesRouteError(
            f"yearbook_catalog 实体类型必须为 region，收到 {entity_type!r}")
    try:
        iid = int(native_code)
    except ValueError:
        raise SeriesRouteError(
            f"indicator 解析失败: yearbook_catalog:{native_code}") from None
    sy, ey = _year_to_range(start, end)
    out = yearbook_read(iid, region=entity_id, start_year=sy, end_year=ey,
                        limit=1000)
    if isinstance(out, dict) and out.get("status") == "domain_unavailable":
        raise SeriesRouteError(
            f"yearbook_catalog 域暂不可用: {out.get('detail', '')}")
    results = out.get("results", []) if isinstance(out, dict) else []
    return [{"indicator_id": f"yearbook_catalog:{native_code}",
             "date": _date_of_year(int(r["year"])), "value": r["value"],
             "unit": r["unit"], "brand": "finddata"} for r in results]


# --- world bank (annual; country is the entity) ------------------------------

def _world_bank_router(native_code: str, entity_type: str, entity_id: str,
                       start: str, end: str) -> list[dict]:
    from fd_find_data_business_mcp.tools_wb import wb_read

    if entity_type != "country":
        raise SeriesRouteError(
            f"world_bank 实体类型必须为 country，收到 {entity_type!r}")
    sy, ey = _year_to_range(start, end)
    out = wb_read(native_code, [entity_id], sy, ey, limit=5000)
    if isinstance(out, dict) and out.get("status") == "domain_unavailable":
        raise SeriesRouteError(f"world_bank 域暂不可用: {out.get('detail', '')}")
    results = out.get("results", []) if isinstance(out, dict) else []
    return [{"indicator_id": f"world_bank:{native_code}",
             "date": _date_of_year(int(r["year"])), "value": r["value"],
             "unit": r["unit"], "brand": "finddata"} for r in results]


#: Explicit router table: source_db -> reader. New stores register here;
#: everything else is an explicit not-supported rejection.
SERIES_ROUTERS: dict[str, Callable[[str, str, Any, str, str], list[dict]]] = {
    "fd_open_data": _semantic_router,
    "yearbook_catalog": _yearbook_router,
    "world_bank": _world_bank_router,
}


def read_series(indicator_id: str, entity_type: str, entity_id: Any,
                start: str, end: str) -> list[dict]:
    """One registry identity -> canonical rows. Raises SeriesRouteError.

    Batch isolation is the caller's job (server.py): one identity's
    rejection must not interrupt the others in the same request.
    """
    sdb, code = split_indicator_id(indicator_id)
    router = SERIES_ROUTERS.get(sdb)
    if router is None:
        raise SeriesRouteError(f"source_db {sdb} 暂不支持序列拉取")
    return router(code, entity_type, entity_id, start, end)
