"""Wenshu (裁判文书) full-text block reader — RustFS zstd blocks, fail-soft.

Judgment bodies are not stored in PostgreSQL. Each month's docs are packed
into zstd-compressed blocks in the S3-compatible RustFS bucket ``wenshu``;
the on-disk format is pinned by fd-law-data's writer
(lib/wenshu/blocks.js, BLOCK_MAGIC ``WENSHUBLOCK1``):

    decompressed block = b"WENSHUBLOCK1\\n"
                       + utf8 JSON {"month": "YYYY-MM",
                                    "docs": [docId, ...],
                                    "offsets": [0, a, a+b, ...]} + b"\\n"
                       + concatenated raw utf-8 doc payloads

``offsets`` has ``len(docs) + 1`` entries: ``offsets[0] == 0`` and
``offsets[i]`` counts payload bytes before doc i, the last entry being the
total payload length. Doc at index i occupies the byte range
``[header-line end + offsets[i], header-line end + offsets[i+1])`` where
"header-line end" is just after the header line's ``\\n``. Lines are
byte-split (the payload itself may contain 0x0a), so the magic and header
lines are located before any whole-buffer decode.

:func:`fetch_judgment_body` fetches one block by its ``block_key``
coordinate (e.g. ``blocks/2024/09/000711.zst``), decompresses, and slices
out one ``doc_id``. Every failure mode — missing/blank coordinates,
missing credentials, network error, bad zstd, bad magic, unparsable or
malformed header, docId not in the block, out-of-bounds slice, invalid
utf-8 — returns ``None`` with a log line and NEVER raises: the same
fail-soft contract as ``domain.query``. ``block_key`` is an internal
coordinate; this module is never customer-facing by itself, tools_wenshu
wraps it.
"""
from __future__ import annotations

import io
import json
import logging
import os
import threading
from typing import Any

import boto3
import zstandard
from botocore.config import Config

logger = logging.getLogger(__name__)

#: First line of every decompressed block (writer: lib/wenshu/blocks.js).
BLOCK_MAGIC = "WENSHUBLOCK1"

_ENDPOINT_ENV = "FDBIZ_RUSTFS_ENDPOINT"
_ACCESS_KEY_ENV = "FDBIZ_RUSTFS_ACCESS_KEY"
_SECRET_KEY_ENV = "FDBIZ_RUSTFS_SECRET_KEY"
_REGION_ENV = "FDBIZ_RUSTFS_REGION"
_BUCKET_ENV = "FDBIZ_RUSTFS_WENSHU_BUCKET"

_DEFAULT_ENDPOINT = "http://100.64.0.11:9000"
_DEFAULT_REGION = "us-east-1"
_DEFAULT_BUCKET = "wenshu"

#: Strict per-call budgets, mirroring domain.py's philosophy: a body read
#: must never hang the MCP transport on a dead object store.
_CONNECT_TIMEOUT_S = 5
_READ_TIMEOUT_S = 15

_client_lock = threading.Lock()
_client_cache: dict[tuple, Any] = {}  # (endpoint, key, secret, region) -> client


def _endpoint() -> str:
    return os.environ.get(_ENDPOINT_ENV) or _DEFAULT_ENDPOINT


def _region() -> str:
    return os.environ.get(_REGION_ENV) or _DEFAULT_REGION


def bucket() -> str:
    """Bucket holding the wenshu body blocks (env-overridable)."""
    return os.environ.get(_BUCKET_ENV) or _DEFAULT_BUCKET


def _client() -> Any:
    """Build (once per credential set) the small S3-compatible client."""
    conf = (
        _endpoint(),
        os.environ.get(_ACCESS_KEY_ENV),
        os.environ.get(_SECRET_KEY_ENV),
        _region(),
    )
    with _client_lock:
        cli = _client_cache.get(conf)
        if cli is None:
            cli = boto3.client(
                "s3",
                endpoint_url=conf[0],
                aws_access_key_id=conf[1],
                aws_secret_access_key=conf[2],
                region_name=conf[3],
                config=Config(
                    connect_timeout=_CONNECT_TIMEOUT_S,
                    read_timeout=_READ_TIMEOUT_S,
                    retries={"max_attempts": 2},
                ),
            )
            _client_cache[conf] = cli
        return cli


def _fetch_compressed(block_key: str) -> bytes | None:
    """GET the zstd block object; None (logged) on any failure."""
    try:
        obj = _client().get_object(Bucket=bucket(), Key=block_key)
        return obj["Body"].read()
    except Exception as exc:  # noqa: BLE001 — fail-soft by contract
        logger.warning("wenshu_body: GET %s failed: %s",
                       block_key, type(exc).__name__)
        return None


def _decompress(compressed: bytes, block_key: str) -> bytes | None:
    """zstd-decompress a block; None (logged) on any failure.

    stream_reader (not decompress()) so frames without a content-size
    header also decompress — the historical writer's framing is not
    guaranteed to carry one.
    """
    try:
        return zstandard.ZstdDecompressor() \
            .stream_reader(io.BytesIO(compressed)).read()
    except Exception as exc:  # noqa: BLE001 — fail-soft by contract
        logger.warning("wenshu_body: zstd decompress %s failed: %s",
                       block_key, type(exc).__name__)
        return None


def _extract(decompressed: bytes, doc_id: str, block_key: str) -> str | None:
    """Slice one doc's utf-8 text out of a decompressed block."""
    nl = decompressed.find(b"\n")
    if nl == -1:
        logger.warning("wenshu_body: %s truncated (no magic newline)", block_key)
        return None
    if decompressed[:nl] != BLOCK_MAGIC.encode("utf-8"):
        logger.warning("wenshu_body: %s bad magic line (expected %s)",
                       block_key, BLOCK_MAGIC)
        return None
    start = nl + 1
    nl2 = decompressed.find(b"\n", start)
    if nl2 == -1:
        logger.warning("wenshu_body: %s truncated (no header newline)", block_key)
        return None
    try:
        header = json.loads(decompressed[start:nl2].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        logger.warning("wenshu_body: %s header is not valid JSON", block_key)
        return None
    docs = header.get("docs") if isinstance(header, dict) else None
    offsets = header.get("offsets") if isinstance(header, dict) else None
    if (not isinstance(docs, list) or not isinstance(offsets, list)
            or len(offsets) != len(docs) + 1):
        logger.warning("wenshu_body: %s malformed header (docs/offsets)", block_key)
        return None
    try:
        idx = docs.index(doc_id)
    except ValueError:
        # Expected miss (doc never in this block), not a malfunction.
        logger.debug("wenshu_body: %s does not contain doc %s", block_key, doc_id)
        return None
    body_start = nl2 + 1
    from_off, to_off = offsets[idx], offsets[idx + 1]
    if (not isinstance(from_off, int) or not isinstance(to_off, int)
            or from_off < 0 or to_off < from_off
            or body_start + to_off > len(decompressed)):
        logger.warning("wenshu_body: %s doc slice out of bounds (doc %s)",
                       block_key, doc_id)
        return None
    try:
        return decompressed[body_start + from_off:body_start + to_off] \
            .decode("utf-8")
    except UnicodeDecodeError:
        logger.warning("wenshu_body: %s doc %s payload is not valid utf-8",
                       block_key, doc_id)
        return None


def fetch_judgment_body(block_key: str, doc_id: str) -> str | None:
    """Read one judgment's body text from its RustFS zstd block.

    Args:
        block_key: internal object coordinate from wenshu_db.judgments
            (e.g. ``blocks/2024/09/000711.zst``). ``block_key IS NULL``
            rows have no body block at all.
        doc_id: the judgment's primary key, looked up inside the block
            header.

    Returns the document's utf-8 text, or ``None`` when anything at all
    goes wrong — unreachable object store, missing credentials, corrupt
    zstd, bad block format, or the doc not being in the block. Never
    raises.
    """
    if not block_key or not doc_id:
        return None
    compressed = _fetch_compressed(block_key)
    if compressed is None:
        return None
    decompressed = _decompress(compressed, block_key)
    if decompressed is None:
        return None
    return _extract(decompressed, doc_id, block_key)
