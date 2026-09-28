"""Registry coverage tool — indicator-registry boundary transparency.

``registry_coverage`` reports, per source, how many native anchors the
unified indicator registry (``fd_open_data.registry_entries``) has
registered and how many of those are verified, alongside the census anchor
counts (``actual_in_source``) and the sources that are deliberately
outside the registry: phase-2 sources (ia35, wenshu_db — not yet
registered) and law_db (document-domain caliber: laws / chunks / guide
cases are document assets, not indicators). Live counts come from one
GROUP BY query; the boundary declarations are static. Fail-soft: when the
registry is unreachable the static part is served with a note and live
counts left null.
"""
from __future__ import annotations

from typing import Any

from fd_find_data_business_mcp.domain import query as domain_query

_DB = "fd_open_data"

#: Census anchors (2026-09-22 普查) — the per-source anchor counts the first
#: registration batch was checked against (task 2.7). ``actual_in_source``
#: and the fail-soft fallback read from here; small drift is natural growth.
_CENSUS_AS_OF = "2026-09-22"

#: Static per-source declarations. Phase values: phase 1 = registered in the
#: first batch; phase 2 = planned, deliberately unregistered; document
#: domain = outside the indicator caliber entirely.
_STATIC_SOURCES: dict[str, dict[str, Any]] = {
    "world_bank": {
        "label_zh": "世界银行 WDI 宏观指标", "phase": "第一期（已登记）",
        "actual_in_source": 1499},
    "yearbook_catalog": {
        "label_zh": "统计年鉴指标目录", "phase": "第一期（已登记）",
        "actual_in_source": 53925},
    "gta_panel": {
        "label_zh": "GTA 上市公司面板变量", "phase": "第一期（已登记）",
        "actual_in_source": 1468},
    "china_city_panel": {
        "label_zh": "中国城市面板变量", "phase": "第一期（已登记）",
        "actual_in_source": 209},
    "fd_open_data": {
        "label_zh": "FindData 镜像活跃概念", "phase": "第一期（已登记）",
        "actual_in_source": 452},
    "ia35": {
        "label_zh": "工商注册（161G，实体为主）", "phase": "第二期（未登记）",
        "actual_in_source": None, "note": "第二期源，未登记"},
    "wenshu_db": {
        "label_zh": "裁判文书（增量暂停）", "phase": "第二期（未登记）",
        "actual_in_source": None, "note": "第二期源，未登记；增量暂停"},
    "law_db": {
        "label_zh": "法律文档域", "phase": "文档域（非指标口径）",
        "actual_in_source": 0,
        "note": "法条/切片/指导案例为文档资产，非指标口径，登记 0 属预期"},
}

_COUNTS_SQL = """
SELECT source_db,
       count(*) AS registered,
       count(*) FILTER (WHERE verified) AS verified,
       count(*) FILTER (WHERE verified IS NOT TRUE) AS unverified
FROM registry_entries
GROUP BY source_db
"""


def _coverage(registered: int | None, actual: int | None) -> float | None:
    """Registration coverage = registered / in-source anchors, 4 decimals."""
    if registered is None or not actual:
        return None
    return round(registered / actual, 4)


def _row(
    source_db: str,
    label_zh: str,
    phase: str,
    registered: int | None,
    verified: int | None,
    unverified: int | None,
    actual_in_source: int | None,
    note: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "source_db": source_db,
        "label_zh": label_zh,
        "phase": phase,
        "registered": registered,
        "verified": verified,
        "unverified": unverified,
        "actual_in_source": actual_in_source,
        "coverage": _coverage(registered, actual_in_source),
    }
    if note:
        out["note"] = note
    return out


def registry_coverage() -> dict:
    """Per-source indicator-registry coverage (live counts + boundary).

    One live GROUP BY over ``fd_open_data.registry_entries`` gives
    registered / verified / unverified per source_db; the static
    declarations supply ``label_zh``, ``phase``, ``actual_in_source`` (the
    2026-09-22 census anchors) and the out-of-registry sources. Returns
    ``{results, count, census_as_of}`` with rows
    ``{source_db, label_zh, phase, registered, verified, unverified,
    actual_in_source, coverage[, note]}``. Fail-soft: when the registry is
    unreachable the static part is served with a ``note`` on the envelope
    and live counts (verified/unverified) left null; registered falls back
    to the census anchor.
    """
    rows = domain_query(_DB, _COUNTS_SQL)
    live: dict[str, dict[str, Any]] = {}
    fallback = isinstance(rows, dict)  # domain_unavailable -> static only
    if not fallback:
        live = {r["source_db"]: r for r in rows}

    results: list[dict[str, Any]] = []
    for source_db, static in _STATIC_SOURCES.items():
        lr = live.get(source_db)
        if lr is not None:
            registered, verified, unverified = (
                lr["registered"], lr["verified"], lr["unverified"])
        elif fallback and static["actual_in_source"]:
            # Census fallback: anchors are registered per the last report;
            # the verified split is what we cannot know without the registry.
            registered, verified, unverified = (
                static["actual_in_source"], None, None)
        else:
            registered, verified, unverified = 0, 0, 0
        results.append(_row(
            source_db, static["label_zh"], static["phase"],
            registered, verified, unverified, static["actual_in_source"],
            static.get("note")))

    # Sources that registered later and are not in the static map yet —
    # surfaced rather than silently hidden (the UNION's live side).
    for source_db, lr in live.items():
        if source_db not in _STATIC_SOURCES:
            results.append(_row(
                source_db, source_db, "已登记（未声明）",
                lr["registered"], lr["verified"], lr["unverified"],
                None, "静态声明缺失；请补充 label/口径"))

    out: dict[str, Any] = {
        "results": results,
        "count": len(results),
        "census_as_of": _CENSUS_AS_OF,
    }
    if fallback:
        out["note"] = ("registry live counts unavailable; static "
                       "declarations served (registered = census anchor)")
    return out
