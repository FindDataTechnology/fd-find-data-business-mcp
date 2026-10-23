"""Tool behavior annotations (mcp-tool-annotations).

Four things must hold, or a client cannot derive an auto-approval policy:

1. **Complete**: every registered tool declares all four hints.
2. **Same set**: the runtime registry and ``annotations.TOOL_ANNOTATIONS``
   name exactly the same tools — a new tool that forgot its entry fails here.
3. **Truthful**: tools that write persistent state are not marked read-only.
   The write set below is an explicit expectation derived from reading the
   implementations, not a value derived from the table — that is what makes
   "implementation changed, declaration did not" detectable.
4. **Per-tool copies**: mutating one tool's annotations must not leak into
   another's.
"""
from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("FDBIZ_JWKS_URI", "")  # no auth in unit tests

from fd_find_data_business_mcp.annotations import (  # noqa: E402
    TOOL_ANNOTATIONS,
    annotations_for,
)
from fd_find_data_business_mcp.server import mcp  # noqa: E402

HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")

#: Tools that write persistent state, read off the implementations:
#:
#: - ``read_range`` cold/stale ranges call the upstream dispatcher, which
#:   upserts observations into the cache (fd_open_data_mcp.fetch.dispatch).
#: - every domain search/list tool with a ``scope`` parameter calls
#:   ``server._scope_record`` -> ``scoping.record_hit``, which increments the
#:   per-(scope, day) counters in ``scope_stats`` on a permitted scoped call.
#:
#: The ``*_read`` / ``*_get_*`` counterparts take no scope and write nothing.
WRITES_STATE = {
    "read_range",
    "indicator_series_read",
    "yearbook_search_indicators",
    "law_search",
    "legal_docs_search",
    "guide_cases_search",
    "wenshu_search",
    "ia35_company_search",
    "wb_search_indicators",
    "gta_search_variables",
    "city_search_variables",
    "daas_list_sources",
    "daas_search_functions",
    "daas_list_indicators",
    "daas_list_rules",
    "daas_search_entities",
}

#: Tools whose result depends on a non-enumerable external set: ``read_range``
#: fetches from a live upstream provider. Everything else reads this service's
#: own versioned stores (crawled cache, federated PG, RustFS body blocks,
#: local embedding model), which are closed and reproducible.
OPEN_WORLD = {"read_range", "indicator_series_read"}

#: Tools that create a new counter row (or bump a counter) per call: replay
#: changes state, so they are not idempotent. ``read_range`` is an upsert on
#: (concept, entity, date, source) — replay converges to the same rows.
NOT_IDEMPOTENT = WRITES_STATE - {"read_range", "indicator_series_read"}


@pytest.fixture(scope="module")
def runtime() -> dict[str, dict]:
    """The four hints as actually served by ``tools/list``."""
    tools = asyncio.run(mcp.list_tools())
    out: dict[str, dict] = {}
    for t in tools:
        assert t.annotations is not None, f"{t.name} declares no annotations"
        dumped = t.annotations.model_dump(by_alias=True)
        out[t.name] = {k: dumped[k] for k in HINTS}
    return out


def test_registry_and_annotation_table_are_the_same_set(runtime):
    assert set(runtime) == set(TOOL_ANNOTATIONS), (
        "runtime registry and annotations.TOOL_ANNOTATIONS differ: a new tool "
        "must be registered in annotations.py"
    )


def test_every_tool_declares_all_four_hints(runtime):
    for name, hints in runtime.items():
        for key in HINTS:
            assert hints.get(key) is not None, f"{name}.{key} is not declared"


def test_writers_are_not_marked_read_only(runtime):
    for name in WRITES_STATE:
        assert runtime[name]["readOnlyHint"] is False, (
            f"{name} writes persistent state but is marked read-only — "
            "clients would auto-approve it"
        )


def test_non_writers_are_read_only(runtime):
    for name, hints in runtime.items():
        if name not in WRITES_STATE:
            assert hints["readOnlyHint"] is True, (
                f"{name} is marked writable but no persistent write was found"
            )


def test_no_tool_is_destructive(runtime):
    """Every writer here is an upsert or a counter increment — none deletes
    or irreversibly overwrites, so destructiveHint is False throughout."""
    for name, hints in runtime.items():
        assert hints["destructiveHint"] is False, f"{name} is marked destructive"


def test_open_world_matches_upstream_reach(runtime):
    for name, hints in runtime.items():
        expected = name in OPEN_WORLD
        assert hints["openWorldHint"] is expected, (
            f"{name}: openWorldHint={hints['openWorldHint']}, expected {expected}"
        )


def test_idempotency_matches_write_shape(runtime):
    for name, hints in runtime.items():
        expected = name not in NOT_IDEMPOTENT
        assert hints["idempotentHint"] is expected, (
            f"{name}: idempotentHint={hints['idempotentHint']}, expected {expected}"
        )


def test_unknown_tool_raises_instead_of_defaulting():
    """A silent default would let an unregistered tool pass."""
    with pytest.raises(KeyError):
        annotations_for("no_such_tool")


def test_annotations_are_copies_not_shared():
    a = annotations_for("read")
    a["readOnlyHint"] = False
    assert annotations_for("read")["readOnlyHint"] is True
    assert annotations_for("list_concepts")["readOnlyHint"] is True
    assert TOOL_ANNOTATIONS["read"]["readOnlyHint"] is True
