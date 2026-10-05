"""Registry coverage tool — indicator-registry boundary transparency.

``registry_coverage`` reports, per source, how many native anchors the
unified indicator registry (``fd_open_data.registry_entries``) has
registered and how many of those are verified, alongside the census anchor
counts (``actual_in_source``) and the sources that are deliberately
outside the registry: ia35 and wenshu_db (served since 2026-09 as
entity-domain / document-domain calibers — registered 0 by design, not
indicator sources) and law_db (document-domain caliber: laws / chunks /
guide cases are document assets, not indicators). Live counts come from
one GROUP BY query; the boundary declarations are static. The envelope
also carries ``calibers`` — the authoritative three-caliber aggregation
(registered / verified / servable, ADR-0002) every presentation surface
must take indicator counts from. Fail-soft: when the registry is
unreachable the static part is served with a note, caliber totals are
null, and nothing raises.
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
        "label_zh": "工商注册（2.88 亿行，逐年快照）", "phase": "已接入（实体域口径）",
        "actual_in_source": None,
        "note": "2026-09 起服务化：企业档案检索 + 注册量聚合；指标登记 0 属预期，"
                "reg_counts 进指标注册目录另行立项"},
    "wenshu_db": {
        "label_zh": "裁判文书（增量暂停）", "phase": "已接入（文档域口径）",
        "actual_in_source": None,
        "note": "2026-09 起服务化：裁判文书检索 + 全文（正文在对象存储）；"
                "指标登记 0 属预期；存量快照，增量暂停"},
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

#: Servable caliber (ADR-0002 three-caliber aggregation): verified registry
#: entries UNION the active concepts whose code no verified entry already
#: covers (cross-source semantic_code equivalence dedupe). Both tables live
#: in the same ``fd_open_data`` database; one scalar row comes back.
#: The dedupe is a NOT EXISTS anti-join, NOT ``code NOT IN (...)``: the live
#: registry carries a verified row with a NULL semantic_code, and one NULL
#: makes ``NOT IN`` exclude every row (measured 2026-10-06: NOT IN gave
#: servable 3,421; NOT EXISTS gives the authoritative 3,421 + 206 = 3,627).
_SERVABLE_SQL = """
SELECT (SELECT count(*) FROM registry_entries WHERE verified)
     + (SELECT count(*) FROM concepts c
        WHERE c.deprecated IS NOT TRUE
          AND NOT EXISTS (SELECT 1 FROM registry_entries r
                          WHERE r.verified AND r.semantic_code = c.code))
     AS servable_total
"""

#: One plain-language composition note per caliber (spec「口径构成可解释」):
#: a reader must be able to tell what each caliber includes/excludes without
#: reading this module.
_CALIBER_NOTES = {
    "registered_total": (
        "登记指标 = registry_entries 全量（verified 与未核验都算）；"
        "目录规模与营销计量口径"),
    "verified_total": (
        "verified 总量 = registry_entries 中已通过语义命名核验（verified）的条目；"
        "「可直读取数」的登记子集"),
    "servable_total": (
        "可服务指标 = verified 登记条目 ∪ 活跃概念（concepts 中 deprecated IS NOT TRUE "
        "且 code 不与任一 verified 条目的 semantic_code 重复）；"
        "「即刻可取数」口径，跨源等价去重"),
}


def _servable_total() -> int | None:
    """The servable caliber count, or None when the query is unusable.

    Fail-soft by shape-check rather than exception: a degraded/unexpected
    response (domain_unavailable dict, wrong row count, missing key) means
    "unknown this call", never a raise.
    """
    rows = domain_query(_DB, _SERVABLE_SQL)
    if (isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict)):
        value = rows[0].get("servable_total")
        if isinstance(value, int):
            return value
    return None


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
    """Per-source indicator-registry coverage (live counts + boundary + calibers).

    One live GROUP BY over ``fd_open_data.registry_entries`` gives
    registered / verified / unverified per source_db; the static
    declarations supply ``label_zh``, ``phase``, ``actual_in_source`` (the
    2026-09-22 census anchors) and the out-of-registry sources. Returns
    ``{results, count, census_as_of, calibers}`` with rows
    ``{source_db, label_zh, phase, registered, verified, unverified,
    actual_in_source, coverage[, note]}``. ``calibers`` is the authoritative
    three-caliber aggregation (ADR-0002, spec「覆盖口径权威聚合」) — the one
    place every presentation surface takes indicator counts from:
    ``registered_total`` (whole registry), ``verified_total``,
    ``servable_total`` (verified entries ∪ active concepts, cross-source
    dedupe), each with a composition note. Fail-soft: when the registry is
    unreachable the static part is served with a ``note`` on the envelope,
    caliber totals are left null with their own note, and nothing raises.
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

    # Three-caliber aggregation (ADR-0002). registered/verified totals are
    # the sums over the GROUP BY — it partitions the whole table, so the
    # sums ARE the full-table counts; servable needs the concepts table and
    # rides its own scalar query.
    calibers: dict[str, Any] = {
        "registered_total": None,
        "verified_total": None,
        "servable_total": None,
        "notes": dict(_CALIBER_NOTES),
    }
    if fallback:
        calibers["note"] = ("registry live counts unavailable; caliber "
                            "totals unknown this call")
    else:
        calibers["registered_total"] = sum(r["registered"] for r in live.values())
        calibers["verified_total"] = sum(r["verified"] for r in live.values())
        servable = _servable_total()
        if servable is None:
            calibers["notes"]["servable_total"] = (
                "servable 口径本次不可知（concepts 聚合查询失败/不可用）；"
                + _CALIBER_NOTES["servable_total"])
        else:
            calibers["servable_total"] = servable

    out: dict[str, Any] = {
        "results": results,
        "count": len(results),
        "census_as_of": _CENSUS_AS_OF,
        "calibers": calibers,
    }
    if fallback:
        out["note"] = ("registry live counts unavailable; static "
                       "declarations served (registered = census anchor)")
    return out
