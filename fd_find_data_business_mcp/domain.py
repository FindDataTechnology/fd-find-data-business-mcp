"""Domain federation layer — read-only access to the Windows data machine.

Serves yearbook/law (and later more) domain databases on the data machine
PG in place. One small engine per database, strict timeouts, fail-soft:
any connection failure or timeout maps to a structured
``{"status": "domain_unavailable"}`` response — the core tools and the
server process are never affected. Data location is configuration
(``FDBIZ_DOMAIN_PG_URL``); moving a database is an env change, not a code
change.
"""
from __future__ import annotations

import os
import threading
from typing import Any

import sqlalchemy
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.pool import QueuePool

#: Databases this build federates. ia35 is deliberately excluded (entity
#: registry, not indicator-shaped; needs its own change).
FEDERATED_DATABASES = ("yearbook_catalog", "law_db")

UNAVAILABLE = "domain_unavailable"

#: Per-call worst case: connect 5s + statement 15s (both overridable for
#: tests / ops tuning).
CONNECT_TIMEOUT_S = int(os.environ.get("FDBIZ_DOMAIN_CONNECT_TIMEOUT", "5"))
STATEMENT_TIMEOUT_MS = int(os.environ.get("FDBIZ_DOMAIN_STATEMENT_TIMEOUT_MS", "15000"))

_engines: dict[str, Engine] = {}
_lock = threading.Lock()


def configured() -> bool:
    return bool(os.environ.get("FDBIZ_DOMAIN_PG_URL"))


def _unavailable(detail: str) -> dict[str, Any]:
    # detail is an exception class name only — never host, SQL, or credentials.
    return {"status": UNAVAILABLE, "detail": detail}


def get_engine(dbname: str) -> Engine:
    """Lazily create (once) the small read pool for a federated database."""
    eng = _engines.get(dbname)
    if eng is not None:
        return eng
    with _lock:
        eng = _engines.get(dbname)
        if eng is not None:
            return eng
        url = sqlalchemy.engine.make_url(os.environ["FDBIZ_DOMAIN_PG_URL"])
        url = url.set(database=dbname)
        eng = sqlalchemy.create_engine(
            url,
            poolclass=QueuePool,
            pool_size=2,
            max_overflow=1,
            pool_pre_ping=True,
            connect_args={
                "connect_timeout": CONNECT_TIMEOUT_S,
                "options": f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
            },
        )
        _engines[dbname] = eng
        return eng


def dispose_all() -> None:
    """Drop cached engines; the next call rebuilds them (tests / recovery).

    A broken cached engine must not block cleanup — each dispose is guarded.
    """
    with _lock:
        for eng in _engines.values():
            try:
                eng.dispose()
            except Exception:  # noqa: BLE001 — cleanup must always complete
                pass
        _engines.clear()


def query(dbname: str, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]] | dict[str, Any]:
    """Run one read query, fail-soft.

    Returns a list of row dicts on success, or a structured
    ``{"status": "domain_unavailable", ...}`` dict on any connection
    failure, timeout, or schema drift. Never raises.
    """
    if not configured():
        return _unavailable("not_configured")
    try:
        with get_engine(dbname).connect() as conn:
            rows = conn.execute(text(sql), params or {}).mappings().all()
            return [dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        # Broad by contract: fail-soft must never raise to the MCP transport.
        # psycopg2 can leak raw errors (e.g. GBK-decoded locale messages on
        # denied CONNECT) outside SQLAlchemyError, so anything reaches here.
        out = _unavailable(type(exc).__name__)
        if os.environ.get("FDBIZ_DOMAIN_DEBUG"):
            out["debug"] = str(exc)[:400]
        return out


# --- Unified indicator registry (fd_open_data.registry_entries) ----------------
#: The registry lives on the same host/user as the federated domain
#: databases, in its own database. Domain tools enrich their search results
#: with registry identity (semantic_code) via this separate lookup — never
#: a cross-database JOIN inside the domain query itself.
REGISTRY_DB = "fd_open_data"

_REGISTRY_LOOKUP_SQL = """
SELECT native_code, semantic_code
FROM registry_entries
WHERE source_db = :sdb
  AND source_table = :stab
  AND source_column = :scol
  AND native_code = ANY(:codes)
  AND verified
  AND semantic_code IS NOT NULL
"""


def registry_semantic_codes(
    source_db: str, source_table: str, source_column: str,
    native_codes: list[str],
) -> dict[str, str]:
    """Map native codes to registry ``semantic_code`` (verified entries only).

    One indexed anchor lookup against ``fd_open_data.registry_entries``.
    Returns ``{}`` on any failure — the same fail-soft contract as
    :func:`query` — so callers serve their rows with ``semantic_code:
    None`` instead of failing. Unverified and unregistered anchors map to
    nothing (the registry quality gate: unverified entries never surface a
    semantic code). Never raises.
    """
    # Dedupe while preserving order; drop blanks/None. Casting to str keeps
    # the yearbook anchor honest (its native_code is the integer id stored
    # as text in the registry).
    codes = [c for c in dict.fromkeys(
        str(c) for c in native_codes if c is not None) if c != ""]
    if not codes:
        return {}
    rows = query(REGISTRY_DB, _REGISTRY_LOOKUP_SQL, {
        "sdb": source_db, "stab": source_table, "scol": source_column,
        "codes": codes,
    })
    if not isinstance(rows, list):
        # Fail-soft: domain_unavailable dict -> rows are served un-enriched.
        return {}
    return {r["native_code"]: r["semantic_code"]
            for r in rows if r.get("semantic_code")}
