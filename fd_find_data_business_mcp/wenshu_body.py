"""Wenshu (裁判文书) full-text block reader — RustFS zstd blocks, fail-soft.

Judgment bodies are not stored in PostgreSQL. Each block packs one month's
docs in the S3-compatible RustFS bucket ``wenshu``. TWO writer formats
exist in the bucket and both are read (sniffed per object):

1. **Historical Windows writer** (every object observed in the bucket as
   of 2026-09-29, incl. the blocks the 2024-2025 re-import writes) —
   reverse-engineered from live objects::

       object = u32-BE header byte length
              + utf8 JSON {"docs": [docId, ...],
                           "offsets": [[start, byteLen], ...],
                           "raw": ..., "src": ...}
              + zstd stream of the concatenated doc payloads

   ``offsets[i]`` is an ABSOLUTE [start, byteLen] pair into the
   DECOMPRESSED payload; doc i's bytes are
   ``payload[offsets[i][0] : offsets[i][0] + offsets[i][1]]``. Payloads
   are raw utf-8 (html docs carry the original ``<!DOCTYPE …`` markup —
   served as stored, the tool discloses ``body_shape``).

2. **blocks.js writer** (fd-law-data lib/wenshu/blocks.js,
   ``BLOCK_MAGIC = "WENSHUBLOCK1"`` — designed while the bucket was
   empty, so no object of this shape exists yet; future crawl-line
   blocks may use it)::

       object = zstd stream of (b"WENSHUBLOCK1\\n"
                                + utf8 JSON {"month", "docs", "offsets"}
                                + b"\\n" + concatenated payloads)

   Here ``offsets`` has ``len(docs) + 1`` cumulative entries and doc i
   occupies ``[header_end+offsets[i], header_end+offsets[i+1])``.

:func:`fetch_judgment_body` fetches one block by its ``block_key``
coordinate (e.g. ``blocks/2024/09/000711.zst``), parses whichever format
the object carries, and slices out one ``doc_id``. Every failure mode —
missing/blank coordinates, missing credentials, network error, bad zstd,
unrecognized format, unparsable or malformed header, docId not in the
block, out-of-bounds slice, invalid utf-8 — returns ``None`` with a log
line and NEVER raises: the same fail-soft contract as
:func:`fd_find_data_business_mcp.domain.query`. ``block_key`` is an
internal coordinate; this module is never customer-facing by itself,
tools_wenshu wraps it.
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


#: Zstd frame magic — an object starting with these bytes is a whole-object
#: zstd stream (blocks.js writer); anything else is the historical
#: length-prefixed-header layout.
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _zstd_read(blob: bytes, block_key: str, what: str) -> bytes | None:
    """zstd-decompress ``blob``; None (logged) on any failure.

    stream_reader (not decompress()) so frames without a content-size
    header also decompress — neither writer's framing is guaranteed to
    carry one.
    """
    try:
        return zstandard.ZstdDecompressor() \
            .stream_reader(io.BytesIO(blob)).read()
    except Exception as exc:  # noqa: BLE001 — fail-soft by contract
        logger.warning("wenshu_body: zstd decompress %s (%s) failed: %s",
                       block_key, what, type(exc).__name__)
        return None


def _parse_block(compressed: bytes, block_key: str) -> tuple[list, Any] | None:
    """Parse a fetched object into (docs, slicer).

    ``slicer(i)`` returns the raw payload bytes of doc ``i`` (raises
    nothing — bounds failures yield None via :func:`_slice`). Recognizes
    both writer formats; returns None (logged) on any malformed input.
    """
    if compressed[:4] == _ZSTD_MAGIC:
        return _parse_blocks_js(compressed, block_key)
    return _parse_legacy(compressed, block_key)


def _parse_blocks_js(compressed: bytes, block_key: str) -> tuple[list, Any] | None:
    """blocks.js layout: whole-object zstd of magic line + JSON + payload."""
    dec = _zstd_read(compressed, block_key, "whole object")
    if dec is None:
        return None
    nl = dec.find(b"\n")
    if nl == -1 or dec[:nl] != BLOCK_MAGIC.encode("utf-8"):
        logger.warning("wenshu_body: %s bad magic line (expected %s)",
                       block_key, BLOCK_MAGIC)
        return None
    start = nl + 1
    nl2 = dec.find(b"\n", start)
    if nl2 == -1:
        logger.warning("wenshu_body: %s truncated (no header newline)", block_key)
        return None
    try:
        header = json.loads(dec[start:nl2].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        logger.warning("wenshu_body: %s header is not valid JSON", block_key)
        return None
    docs = header.get("docs") if isinstance(header, dict) else None
    offsets = header.get("offsets") if isinstance(header, dict) else None
    if (not isinstance(docs, list) or not isinstance(offsets, list)
            or len(offsets) != len(docs) + 1):
        logger.warning("wenshu_body: %s malformed header (docs/offsets)", block_key)
        return None
    payload_start = nl2 + 1

    def slicer(i: int) -> bytes | None:
        a, b = offsets[i], offsets[i + 1]
        if (not isinstance(a, int) or not isinstance(b, int)
                or a < 0 or b < a or payload_start + b > len(dec)):
            logger.warning("wenshu_body: %s doc slice out of bounds (doc %s)",
                           block_key, docs[i])
            return None
        return dec[payload_start + a:payload_start + b]

    return docs, slicer


def _parse_legacy(compressed: bytes, block_key: str) -> tuple[list, Any] | None:
    """Historical Windows writer: u32-BE header length + JSON + zstd payload.

    Live-decoded 2026-09-29 from blocks/2024/09/000711.zst and
    blocks/2025/10/000020.zst: header JSON has ``docs`` and ``offsets``
    as per-doc ``[absolute_start, byte_len]`` pairs into the decompressed
    payload (plus opaque ``raw``/``src`` fields, ignored here).
    """
    if len(compressed) < 5:
        logger.warning("wenshu_body: %s too short for legacy header", block_key)
        return None
    plen = int.from_bytes(compressed[:4], "big")
    if plen <= 0 or 4 + plen >= len(compressed):
        logger.warning("wenshu_body: %s implausible legacy header length %d",
                       block_key, plen)
        return None
    try:
        header = json.loads(compressed[4:4 + plen].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        logger.warning("wenshu_body: %s header is not valid JSON", block_key)
        return None
    docs = header.get("docs") if isinstance(header, dict) else None
    offsets = header.get("offsets") if isinstance(header, dict) else None
    if (not isinstance(docs, list) or not isinstance(offsets, list)
            or len(offsets) != len(docs)):
        logger.warning("wenshu_body: %s malformed header (docs/offsets)", block_key)
        return None
    payload = _zstd_read(compressed[4 + plen:], block_key, "payload section")
    if payload is None:
        return None

    def slicer(i: int) -> bytes | None:
        pair = offsets[i]
        if (not isinstance(pair, list) or len(pair) != 2
                or not all(isinstance(v, int) for v in pair)
                or pair[0] < 0 or pair[1] < 0
                or pair[0] + pair[1] > len(payload)):
            logger.warning("wenshu_body: %s doc slice out of bounds (doc %s)",
                           block_key, docs[i])
            return None
        return payload[pair[0]:pair[0] + pair[1]]

    return docs, slicer


def _decode_doc(raw: bytes | None, doc_id: str, block_key: str) -> str | None:
    """utf-8 decode one sliced payload; None (logged) on failure."""
    if raw is None:
        return None
    try:
        return raw.decode("utf-8")
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
    parsed = _parse_block(compressed, block_key)
    if parsed is None:
        return None
    docs, slicer = parsed
    try:
        idx = docs.index(doc_id)
    except ValueError:
        # Expected miss (doc never in this block), not a malfunction.
        logger.debug("wenshu_body: %s does not contain doc %s", block_key, doc_id)
        return None
    return _decode_doc(slicer(idx), doc_id, block_key)
