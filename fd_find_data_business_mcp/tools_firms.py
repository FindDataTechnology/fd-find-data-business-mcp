"""ia35 工商注册 domain tools over the federated ia35 database (finddata-branded).

ia35 (on the same Windows data machine PG as the other federated domains,
read-only role ``fdbiz_ro``) holds China's business-registry corpus:
``firms_full`` is a ~288M-row / 141GB pile of YEARLY SNAPSHOT batches — the
``数据源`` column is a four-digit batch year (2023 back through at least
1995, live-probed 2026-09-29; each batch a full re-snapshot), not a
per-row source label. One company therefore appears once per batch it
survived into; dedup is part of every answer (credit-code lookup: newest
batch wins, ties by 核准日期 desc; name search: pinned to the newest
snapshot batch, Python-side dedup by credit code — falling back to
(企业名称, 登记机关) when the code is blank).

Decode-JOIN vocabulary (live ia35, 2026-09-29, ``SELECT DISTINCT "列"
FROM strl_texts``): exactly three values — 经营范围 / 注册地址 / 曾用名 —
matching firms_full's 经营范围引用 / 注册地址引用 / 曾用名引用 ints one to
one, resolved via strl_texts UNIQUE(数据源, 列, ref_key). Refs are LEFT
JOINed with the batch's own 数据源; a missing or unreferenced decode
surfaces as null honestly. The raw ``*引用`` ints and the 数据源/行坐标
internal keys never leave this module — SQL does not select them and the
projection whitelist would drop them even if a row carried them.

Date encodings (live-verified 2026-09-29, cross-checked against 成立年份
across the 2023/2019/2010/2004 batches): 成立日期/核准日期 are NOT
YYYYMMDD ints — they are Excel-serial day counts on the 1960-01-01 epoch
(e.g. 23208 → 2023-07-17; 成立年份 agrees on every probed row). est_date
is normalized to a YYYYMMDD string when the int reads as a plausible
calendar YYYYMMDD, or decodes (1960 epoch) into 1900–2100 — otherwise
null, as-is.

Snapshot-window disclosure: 名称搜索 serves the newest snapshot batch
only (default 2023, ``FDBIZ_IA35_SNAPSHOT_YEAR`` override). Companies
outside that batch are reachable ONLY by credit-code exact lookup. ia35
is a static snapshot: when a new year batch is loaded, ``_SNAPSHOT_YEAR``
(exported for operators) and the 企业名称 partial index must be advanced
in step. Every payload carries ``brand: "finddata"``.

Index/plan status (EXPLAIN, 2026-09-29): idx_firms_full_code is VALID
(7.9GB) and the credit-code path plans as an index scan; 统一社会信用代码
column stats remain stale (n_distinct ~58k), which inflates row estimates
but not results — an operator-side ANALYZE will tidy the plans. The
企业名称 partial index (数据源='2023') is operator-pending: until it
lands, name-prefix queries scan the 13M-row snapshot batch and can exceed
the 15s federated statement timeout (honest ``domain_unavailable``). The
SQL below is deliberately shaped WITHOUT ``LIMIT 1`` (all batch rows
fetched, newest picked in Python): with ``LIMIT 1`` the planner keeps a
backward idx_firms_full_src scan trap even with a valid code index, while
the no-LIMIT shape deterministically plans as code-index scan + tiny
sort. reg_counts DOES plan cleanly: province+year filters BitmapOr two
idx_reg_counts_prov_year scans on the RAW 成立年份 column, with the
serial-date conversion riding as a filter.
"""
from __future__ import annotations

import datetime
import os
from typing import Any

from fd_find_data_business_mcp.domain import query

_BRAND = "finddata"
_DB = "ia35"

#: Raw dictionary refs and internal coordinates that must never reach a
#: customer payload. SQL never selects them; this filter keeps it that way.
_FORBIDDEN_KEYS = frozenset({
    "经营范围引用", "注册地址引用", "曾用名引用",
    "数据源", "est_date_raw", "bad_total",
})

_MAX_SEARCH_LIMIT = 200
_MAX_REGCOUNTS_LIMIT = 5000

#: firms_full 成立日期 / 核准日期 serial epoch (live-verified, see module
#: docstring) and the reg_counts 成立年份 serial epoch fixed by contract
#: (anchor 32741 → 1989, live-verified 2026-09-29).
_FIRM_DATE_EPOCH = datetime.date(1960, 1, 1)
_REGCOUNT_EPOCH_SQL = "DATE '1899-12-30'"
#: Excel-serial → plausible-year window; anything outside is dropped and
#: counted into the response ``note``.
_MIN_YEAR, _MAX_YEAR = 1900, 2100
#: 成立年份 values up to this are true years; above are Excel serials.
_TRUE_YEAR_CEILING = 2027

_SNAPSHOT_ENV = "FDBIZ_IA35_SNAPSHOT_YEAR"
_DEFAULT_SNAPSHOT_YEAR = "2023"

#: Disclosed alongside every batch-scoped answer: ia35 is a yearly static
#: snapshot corpus, so no answer describes live registry state.
_SNAPSHOT_CALIBER = (
    "历史批次快照，非实时登记状态；批次年份见 snapshot_year/batch_year"
)

#: reg_counts carries four industry granularities; 门类 is the coarsest
#: (single letter) and the only one the original API exposed.
_INDUSTRY_LEVELS = frozenset({"门类", "大类", "中类", "小类"})

#: Snapshot batch name-paths serve — the module constant the search SQL is
#: pinned to. Resolved once from ``FDBIZ_IA35_SNAPSHOT_YEAR`` (read-once
#: cache); operators advance it when a new yearly batch is loaded.
_SNAPSHOT_YEAR: str | None = None


def snapshot_year() -> str:
    """Resolve the snapshot batch (env override, read once, cached)."""
    global _SNAPSHOT_YEAR
    if _SNAPSHOT_YEAR is None:
        _SNAPSHOT_YEAR = (
            os.environ.get(_SNAPSHOT_ENV) or _DEFAULT_SNAPSHOT_YEAR
        ).strip() or _DEFAULT_SNAPSHOT_YEAR
    return _SNAPSHOT_YEAR

#: Two decode registries coexist in ia35 and are NOT interchangeable:
#:
#: - ``strl_texts`` (UNIQUE(数据源, 列, ref_key)) — batch-scoped字典 for the
#:   three long-text columns: 经营范围 / 注册地址 / 曾用名. A ref resolves
#:   only against ITS OWN batch's 数据源, so these joins carry the batch.
#: - ``firm_dict`` (PK(列, id), UNIQUE(列, txt)) — global字典 for the short
#:   enum-ish columns: 企业类型 / 经营状态 / 登记机关 / 营业期限. No batch
#:   dimension: an id means the same text in every snapshot.
#:
#: 2026-10-12 live schema probe: firms_full has NO 企业类型 / 经营状态 /
#: 登记机关 text columns — only their ``*引用`` integer counterparts
#: (alongside 营业期限引用). Selecting the text names is a ProgrammingError;
#: the pre-fix tool did exactly that and could never serve a row. The enum
#: refs below resolve through firm_dict; long text stays on strl_texts.
#: 曾用名 also has a real text column (f."曾用名") — the strl_texts arm
#: remains the richer decode and keeps winning via COALESCE.

#: strl_texts decode arms: alias -> (列 literal, firms_full ref column).
#: The 列 literals are the live-confirmed DISTINCT vocabulary, verbatim.
_DECODE_JOINS = (
    ("scope", "经营范围", "经营范围引用"),
    ("addr", "注册地址", "注册地址引用"),
    ("former", "曾用名", "曾用名引用"),
)

#: firm_dict decode arms: alias -> (列 literal, firms_full ref column).
#: Batch-independent — no 数据源 predicate in the join.
_DICT_JOINS = (
    ("ctype", "企业类型", "企业类型引用"),
    ("cstatus", "经营状态", "经营状态引用"),
    ("cregistrar", "登记机关", "登记机关引用"),
)

#: Whitelist: hits carry only these fields — decoded dictionary text, never
#: the raw refs, never batch/row coordinates.
_RESULT_FIELDS = (
    "credit_code", "name", "legal_rep", "reg_capital", "paid_capital",
    "est_date", "est_year", "status", "company_type", "registrar",
    "province", "city", "county", "address", "business_scope",
    "former_names", "tags",
)

_REGCOUNT_FIELDS = (
    "year", "province", "city", "county", "industry", "industry_code",
    "new_firms",
)

_FIRM_SELECT = (
    'SELECT f."统一社会信用代码" AS credit_code, f."企业名称" AS name,\n'
    '       COALESCE(NULLIF(f."法定代表人", \'\'), NULLIF(f."法人代表", \'\')) AS legal_rep,\n'
    '       f."注册资本" AS reg_capital, f."实缴资本" AS paid_capital,\n'
    '       f."成立日期" AS est_date_raw, f."成立年份" AS est_year,\n'
    '       cstatus.txt AS status, ctype.txt AS company_type,\n'
    '       cregistrar.txt AS registrar,\n'
    '       f."省份" AS province, f."城市" AS city, f."区县" AS county,\n'
    '       addr.txt AS address, scope.txt AS business_scope,\n'
    '       COALESCE(former.txt, NULLIF(f."曾用名", \'\')) AS former_names,\n'
    '       f."标签" AS tags,\n'
    '       f."数据源" AS batch_year\n'
    'FROM firms_full f\n'
)

_DECODE_JOIN_SQL = "\n".join(
    f'LEFT JOIN strl_texts {alias} ON {alias}."数据源" = f."数据源"'
    f' AND {alias}."列" = \'{col}\''
    f' AND {alias}.ref_key = f."{ref}"'
    for alias, col, ref in _DECODE_JOINS
)

_DICT_JOIN_SQL = "\n".join(
    f'LEFT JOIN firm_dict {alias} ON {alias}."列" = \'{col}\''
    f' AND {alias}.id = f."{ref}"'
    for alias, col, ref in _DICT_JOINS
)

#: Both decode layers ride every firm query; a missing dictionary entry
#: surfaces as null, never as a raw ref and never as a guess.
_ALL_JOIN_SQL = _DECODE_JOIN_SQL + "\n" + _DICT_JOIN_SQL


def _escape_like(q: str) -> str:
    """Escape LIKE/ILIKE metacharacters so user input matches literally."""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _norm_est_date(v: Any) -> str | None:
    """Normalize 成立日期 to a YYYYMMDD string, or None when undecodable.

    Two encodings coexist per the live data: plausible calendar YYYYMMDD
    ints are served verbatim; smaller ints are 1960-epoch Excel serials
    decoded against _FIRM_DATE_EPOCH. Anything decoding outside
    1900–2100 (or not a calendar date at all) is null, as-is.
    """
    if v is None or isinstance(v, bool):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    if 19000101 <= n <= 20991231:  # YYYYMMDD-shaped
        y, m, d = n // 10000, n // 100 % 100, n % 100
        try:
            datetime.date(y, m, d)
        except ValueError:
            pass
        else:
            return str(n)
    if not -60000 <= n <= 60000:  # cannot decode into 1900-2100; no overflow
        return None
    decoded = _FIRM_DATE_EPOCH + datetime.timedelta(days=n)
    if _MIN_YEAR <= decoded.year <= _MAX_YEAR:
        return decoded.strftime("%Y%m%d")
    return None


def _batch_year(v: Any) -> int | None:
    """数据源 text batch → int year; unparseable batches stay null."""
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _result(row: dict[str, Any]) -> dict[str, Any]:
    """Project one firms_full hit onto the public whitelist."""
    out: dict[str, Any] = {k: row.get(k) for k in _RESULT_FIELDS}
    out["est_date"] = _norm_est_date(row.get("est_date_raw"))
    out["batch_year"] = _batch_year(row.get("batch_year"))
    out["brand"] = _BRAND
    return {k: v for k, v in out.items() if k not in _FORBIDDEN_KEYS}


def ia35_company_search(
    name_prefix: str | None = None, credit_code: str | None = None,
    limit: int = 20, company_type: str | None = None,
) -> dict[str, Any]:
    """Search the business registry: exact credit-code lookup or name prefix.

    Two paths, at least one required:

    - ``credit_code`` — exact 统一社会信用代码 match across ALL snapshot
      batches; the newest batch wins (ties by 核准日期 desc), so one
      company resolves to one row. When both parameters are given the
      code path takes precedence and ``name_prefix`` is ignored (noted in
      the response).
    - ``name_prefix`` — 企业名称 prefix match pinned to the newest
      snapshot batch (``snapshot_year()``, default 2023), deduped in
      Python by credit code (blank-code rows by (企业名称, 登记机关)).

    ``company_type`` (optional) filters on the DECODED 企业类型 text
    (firm_dict dictionary, e.g. "个体工商户"), so callers can isolate a
    registration form — individual businesses are roughly half the corpus
    and are the intended target of the micro-business preset line. It
    applies to both paths. The response echoes it back as ``company_type``
    so an empty result is attributable to the filter rather than to a
    silent miss.

    Batch-window disclosure: 名称搜索服务最新快照批次（默认 2023），批次外
    企业仅支持信用代码精确查；ia35 为静态快照，新年份批次载入后需同步
    _SNAPSHOT_YEAR 与企业名称部分索引。**该批次是历史快照，不是实时登记
    状态**——响应以 ``snapshot_year`` + ``snapshot_caliber`` 明示，调用方
    不得将其呈现为当前登记状态。 At most 200 rows (default 20 —
    registry rows are wide). est_date is a YYYYMMDD string or null;
    reg_capital/paid_capital are numeric or null; decodes (address,
    business_scope, former_names) are null where the dictionary has no
    entry — never guessed. Returns
    ``{results: [...], count, truncated, brand}`` (name path adds
    ``snapshot_year``). Operational envelope: name-prefix queries scan the
    whole snapshot batch until the 企业名称 partial index lands
    (operator-pending), so they can exceed the 15s federated timeout and
    surface as ``{"status": "domain_unavailable"}``; the credit-code path
    is index-backed (idx_firms_full_code VALID). A federation failure
    passes through as ``{"status": "domain_unavailable"}``.
    """
    cc = (credit_code or "").strip()
    np = (name_prefix or "").strip()
    if not cc and not np:
        return {"status": "invalid_params", "brand": _BRAND}
    limit = min(max(int(limit), 1), _MAX_SEARCH_LIMIT)
    ctype = (company_type or "").strip() or None
    # Predicate on the decoded text, not the ref int: the dictionary is the
    # only place the human-readable type lives, and filtering on it keeps
    # the filter and the projected field provably the same value.
    type_sql = ' AND ctype.txt = :ctype' if ctype is not None else ""
    type_params = {"ctype": ctype} if ctype is not None else {}
    echo_type = {"company_type": ctype} if ctype is not None else {}
    if cc:
        # All batches for this code, newest first — Python takes row 0.
        # Deliberately no LIMIT 1: see module docstring for the EXPLAIN
        # rationale (batch rows ≤ number of yearly snapshots, trivial).
        sql = (
            _FIRM_SELECT + _ALL_JOIN_SQL + "\n"
            'WHERE f."统一社会信用代码" = :code' + type_sql + "\n"
            'ORDER BY f."数据源" DESC, f."核准日期" DESC NULLS LAST'
        )
        rows = query(_DB, sql, {"code": cc, **type_params})
        if isinstance(rows, dict):
            return rows
        out: dict[str, Any] = {
            "results": [_result(rows[0])] if rows else [],
            "count": 1 if rows else 0,
            "truncated": False,
            "snapshot_year": snapshot_year(),
            "snapshot_caliber": _SNAPSHOT_CALIBER,
            "brand": _BRAND,
        }
        out.update(echo_type)
        if np:
            out["note"] = "已按统一社会信用代码精确查询，忽略 name_prefix 参数。"
        return out
    # Name-prefix path: pinned to one snapshot batch.
    esc = _escape_like(np)
    params: dict[str, Any] = {
        "prefix": f"{esc}%",
        "snapshot": snapshot_year(),
        "lim": limit + 1,
    }
    sql = (
        _FIRM_SELECT + _ALL_JOIN_SQL + "\n"
        'WHERE f."数据源" = :snapshot AND f."企业名称" LIKE :prefix'
        + type_sql + "\n"
        'ORDER BY f."企业名称"\nLIMIT :lim'
    )
    rows = query(_DB, sql, {**params, **type_params})
    if isinstance(rows, dict):
        return rows
    # Dedup inside the snapshot batch: code first, else (name, registrar).
    seen: set[Any] = set()
    deduped: list[dict[str, Any]] = []
    for r in rows:
        key = r.get("credit_code") or (
            (r.get("name"), r.get("registrar")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    truncated = len(deduped) > limit
    return {
        "results": [_result(r) for r in deduped[:limit]],
        "count": min(len(deduped), limit),
        "truncated": truncated,
        "snapshot_year": params["snapshot"],
        "snapshot_caliber": _SNAPSHOT_CALIBER,
        **echo_type,
        "brand": _BRAND,
    }


def ia35_reg_counts_read(
    start_year: int | None = None, end_year: int | None = None,
    province: str | None = None, industry: str | None = None,
    limit: int = 500, city: str | None = None, county: str | None = None,
    industry_class: str | None = None, industry_level: str = "门类",
) -> dict[str, Any]:
    """Read 新增注册企业数量 aggregates from reg_counts (one row per bucket).

    Filters (all optional, combinable): ``start_year``/``end_year`` closed
    interval on the NORMALIZED year, ``province`` equality, ``industry``
    equality against 行业门类代码 OR 行业门类 name. Year normalization (in
    SQL, one scan): 成立年份 ≤ 2027 is a true year; above that it is an
    Excel serial on the 1899-12-30 epoch (anchor: 32741 → 1989). Serials
    decoding outside 1900–2100 are dropped and disclosed as
    ``"N 行年份异常被排除"`` in ``note``. LIMIT clamped 1..5000, ordered by
    year then province. Missing buckets stay missing — nothing is
    zero-filled. Returns
    ``{results: [{year, province, city, county, industry, industry_code,
    new_firms, brand}], count, truncated, note?}``.

    Finer geography and industry (added 2026-10-12 for the micro-business
    line — "这条街这个行业开了几家"): ``city`` / ``county`` equality, and
    ``industry_class`` matched at the level named by ``industry_level``
    (one of 门类 / 大类 / 中类 / 小类, default 门类) against that level's
    代码 OR 名称 — e.g. industry_level="小类" with "快餐服务" or "H6220".
    The four industry levels and 省份/城市/区县 all exist in the table; only
    some are index-backed.

    **Query guard**: only ``province`` and ``成立年份`` carry indexes
    (idx_reg_counts_prov_year, idx_reg_counts_year). Filtering by city /
    county / industry alone forces a full 12.4M-row scan (measured 10.9s
    cold, near the 15s federated timeout), so ``city`` or ``county``
    REQUIRES ``province`` — the tool returns ``invalid_params`` rather than
    serving a query that predictably times out. Measured plans:
    province+year+county+小类 ≈ 3.8s (index scan, safe). A composite index
    on (省份, 城市, 区县, 行业小类代码) would remove the restriction and is
    operator-side work (fdbiz_ro cannot CREATE INDEX).

    Plan note (EXPLAIN 2026-09-29): the raw-column branch
    ``(成立年份 <= 2027 AND 成立年份 BETWEEN …) OR (成立年份 > 2027)``
    keeps province+year filters on the idx_reg_counts_prov_year bitmap;
    the converted-year interval rides as a post-filter. Unfiltered or
    province-less sweeps scan the full 12.4M-row table and can exceed the
    15s federated timeout — narrow with ``province`` and/or the year
    bounds. A federation failure passes through as
    ``{"status": "domain_unavailable"}``.
    """
    limit = min(max(int(limit), 1), _MAX_REGCOUNTS_LIMIT)
    y1 = int(start_year) if start_year is not None else None
    y2 = int(end_year) if end_year is not None else None
    prov = (province or "").strip() or None
    ind = (industry or "").strip() or None
    ct = (city or "").strip() or None
    co = (county or "").strip() or None
    iclass = (industry_class or "").strip() or None
    # Only 省份/成立年份 are indexed; without a province anchor a city or
    # county filter degenerates into a 12.4M-row scan that likely exceeds
    # the federated timeout — refuse rather than serve a predictably-dead
    # query (see docstring for measured plans).
    if (ct is not None or co is not None) and prov is None:
        return {
            "status": "invalid_params",
            "detail": "city/county 过滤必须同时提供 province（无省份锚点会全表扫描并超时）",
            "brand": _BRAND,
        }
    if iclass is not None:
        lvl = (industry_level or "门类").strip()
        if lvl not in _INDUSTRY_LEVELS:
            return {
                "status": "invalid_params",
                "detail": (
                    f"industry_level 必须是 {sorted(_INDUSTRY_LEVELS)} 之一，"
                    f"收到 {lvl!r}"
                ),
                "brand": _BRAND,
            }

    params: dict[str, Any] = {"lim": limit + 1}
    where = ""
    if prov is not None:
        params["province"] = prov
        where += ' AND "省份" = :province'
    if ct is not None:
        params["city"] = ct
        where += ' AND "城市" = :city'
    if co is not None:
        params["county"] = co
        where += ' AND "区县" = :county'
    if ind is not None:
        params["industry"] = ind
        where += ' AND ("行业门类代码" = :industry OR "行业门类" = :industry)'
    if iclass is not None:
        params["iclass"] = iclass
        where += (
            f' AND ("行业{lvl}代码" = :iclass OR "行业{lvl}" = :iclass)'
        )
    raw_bounds = ""
    if y1 is not None:
        params["y1"] = y1
        raw_bounds += ' AND "成立年份" >= :y1'
    if y2 is not None:
        params["y2"] = y2
        raw_bounds += ' AND "成立年份" <= :y2'
    if raw_bounds:
        where += (' AND (("成立年份" <= ' + str(_TRUE_YEAR_CEILING)
                  + raw_bounds + ')\n'
                  '     OR ("成立年份" > ' + str(_TRUE_YEAR_CEILING) + '))')
    conv_bounds = ""
    if y1 is not None:
        conv_bounds += " AND w.year >= :y1"
    if y2 is not None:
        conv_bounds += " AND w.year <= :y2"

    sql = (
        "SELECT year, province, city, county, industry, industry_code,\n"
        "       new_firms, bad_total\n"
        "FROM (\n"
        "  SELECT s.*,\n"
        # Window layer: counts anomalies over every province/industry-
        # matching row — including the bad-year ones the outer WHERE then
        # drops — so the disclosure costs no second scan.
        "         count(*) FILTER (WHERE s.bad_year) OVER () AS bad_total\n"
        "  FROM (\n"
        "  SELECT\n"
        "    CASE WHEN \"成立年份\" <= " + str(_TRUE_YEAR_CEILING) + "\n"
        "         THEN \"成立年份\"::int\n"
        "         WHEN EXTRACT(year FROM " + _REGCOUNT_EPOCH_SQL
        + ' + "成立年份") BETWEEN ' + str(_MIN_YEAR) + " AND " + str(_MAX_YEAR) + "\n"
        "         THEN EXTRACT(year FROM " + _REGCOUNT_EPOCH_SQL
        + ' + "成立年份")::int\n'
        "    END AS year,\n"
        '    ("成立年份" > ' + str(_TRUE_YEAR_CEILING) + " AND EXTRACT(year FROM "
        + _REGCOUNT_EPOCH_SQL + ' + "成立年份") NOT BETWEEN '
        + str(_MIN_YEAR) + " AND " + str(_MAX_YEAR) + ") AS bad_year,\n"
        '    "省份" AS province, "城市" AS city, "区县" AS county,\n'
        '    "行业门类" AS industry, "行业门类代码" AS industry_code,\n'
        '    "新增注册企业数量" AS new_firms\n'
        "  FROM reg_counts\n"
        "  WHERE TRUE" + where + "\n"
        "  ) s\n"
        ") w\n"
        "WHERE w.year IS NOT NULL" + conv_bounds + "\n"
        "ORDER BY year, province\n"
        "LIMIT :lim"
    )
    rows = query(_DB, sql, params)
    if isinstance(rows, dict):
        return rows
    truncated = len(rows) > limit
    years = [int(r["year"]) for r in rows[:limit]
             if r.get("year") is not None]
    out: dict[str, Any] = {
        "results": [
            {**{k: r.get(k) for k in _REGCOUNT_FIELDS}, "brand": _BRAND}
            for r in rows[:limit]
        ],
        "count": min(len(rows), limit),
        "truncated": truncated,
        # Disclosed, not inferred: reg_counts has no batch column, so the
        # newest year actually served is the honest freshness statement.
        "latest_year_available": max(years) if years else None,
        "snapshot_caliber": _SNAPSHOT_CALIBER,
        "brand": _BRAND,
    }
    bad = max((int(r.get("bad_total") or 0) for r in rows), default=0)
    if bad:
        out["note"] = f"{bad} 行年份异常被排除"
    return out
