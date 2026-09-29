"""Tests for the wenshu body-block reader (wenshu_body.py).

Unit tests build real-format block bytes (magic + JSON header + payloads,
zstd-compressed per lib/wenshu/blocks.js) and fake the S3 client — no
network, no credentials needed.
"""
from __future__ import annotations

import json

import pytest
import zstandard

import fd_find_data_business_mcp.wenshu_body as wb


def _make_block(docs: dict[str, str], month: str = "2024-09") -> bytes:
    """Build a block exactly as the writer does (blocks.js buildRawBlock)."""
    payloads = [text.encode("utf-8") for text in docs.values()]
    offsets = [0]
    for buf in payloads:
        offsets.append(offsets[-1] + len(buf))
    header = {"month": month, "docs": list(docs), "offsets": offsets}
    raw = (f"{wb.BLOCK_MAGIC}\n{json.dumps(header)}\n".encode("utf-8")
           + b"".join(payloads))
    return zstandard.ZstdCompressor(level=19).compress(raw)


class _FakeBody:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


class _FakeS3:
    def __init__(self, objects: dict[str, bytes] | None = None,
                 error: Exception | None = None):
        self.objects = objects or {}
        self.error = error
        self.calls: list[dict] = []

    def get_object(self, Bucket: str, Key: str) -> dict:
        self.calls.append({"Bucket": Bucket, "Key": Key})
        if self.error is not None:
            raise self.error
        if Key not in self.objects:
            raise KeyError(Key)  # NoSuchKey-shaped
        return {"Body": _FakeBody(self.objects[Key])}


@pytest.fixture()
def s3(monkeypatch):
    fake = _FakeS3()
    monkeypatch.setattr(wb, "_client", lambda: fake)
    return fake


class TestFetchJudgmentBody:
    def test_hit_returns_exact_utf8_text(self, s3, monkeypatch):
        s3.objects["blocks/2024/09/000711.zst"] = _make_block({
            "doc-a": "本院认为，合同合法有效。",
            "doc-b": "判决如下：驳回上诉，维持原判。",
        })
        monkeypatch.setenv("FDBIZ_RUSTFS_WENSHU_BUCKET", "wenshu")
        out = wb.fetch_judgment_body("blocks/2024/09/000711.zst", "doc-b")
        assert out == "判决如下：驳回上诉，维持原判。"

    def test_get_object_uses_bucket_and_key_verbatim(self, s3):
        s3.objects["blocks/2024/01/000001.zst"] = _make_block({"d1": "正文"})
        assert wb.fetch_judgment_body("blocks/2024/01/000001.zst", "d1") == "正文"
        assert s3.calls == [{"Bucket": "wenshu",
                             "Key": "blocks/2024/01/000001.zst"}]

    def test_doc_not_in_block_returns_none(self, s3):
        s3.objects["blocks/2024/09/000711.zst"] = _make_block({"doc-a": "甲文"})
        assert wb.fetch_judgment_body("blocks/2024/09/000711.zst", "doc-zz") is None

    def test_bad_magic_returns_none(self, s3):
        raw = b"WENSNUBLOCK1\n{\"month\":\"2024-09\",\"docs\":[\"d\"],\"offsets\":[0,2]}\nxx"
        s3.objects["blocks/2024/09/000711.zst"] = zstandard.ZstdCompressor() \
            .compress(raw)
        assert wb.fetch_judgment_body("blocks/2024/09/000711.zst", "d") is None

    def test_bad_zstd_returns_none(self, s3):
        s3.objects["blocks/2024/09/000711.zst"] = b"this is not zstd"
        assert wb.fetch_judgment_body("blocks/2024/09/000711.zst", "d") is None

    def test_bad_header_json_returns_none(self, s3):
        raw = wb.BLOCK_MAGIC.encode("utf-8") + b"\n{not json}\nxx"
        s3.objects["blocks/2024/09/000711.zst"] = zstandard.ZstdCompressor() \
            .compress(raw)
        assert wb.fetch_judgment_body("blocks/2024/09/000711.zst", "d") is None

    def test_malformed_header_offsets_return_none(self, s3):
        header = json.dumps({"month": "2024-09", "docs": ["d"],
                             "offsets": [0]})  # needs len(docs)+1 entries
        raw = f"{wb.BLOCK_MAGIC}\n{header}\nxx".encode("utf-8")
        s3.objects["blocks/2024/09/000711.zst"] = zstandard.ZstdCompressor() \
            .compress(raw)
        assert wb.fetch_judgment_body("blocks/2024/09/000711.zst", "d") is None

    def test_slice_out_of_bounds_returns_none(self, s3):
        header = json.dumps({"month": "2024-09", "docs": ["d"],
                             "offsets": [0, 9999]})
        raw = f"{wb.BLOCK_MAGIC}\n{header}\nxx".encode("utf-8")
        s3.objects["blocks/2024/09/000711.zst"] = zstandard.ZstdCompressor() \
            .compress(raw)
        assert wb.fetch_judgment_body("blocks/2024/09/000711.zst", "d") is None

    def test_network_error_returns_none(self, monkeypatch):
        fake = _FakeS3(error=ConnectionError("object store unreachable"))
        monkeypatch.setattr(wb, "_client", lambda: fake)
        assert wb.fetch_judgment_body("blocks/2024/09/000711.zst", "d") is None

    def test_missing_object_returns_none(self, s3):
        assert wb.fetch_judgment_body("blocks/2033/01/999999.zst", "d") is None

    def test_blank_coordinates_return_none_without_calling_s3(self, s3):
        for block_key, doc_id in (("", "d"), (None, "d"), ("k", ""),
                                  ("k", None)):
            assert wb.fetch_judgment_body(block_key, doc_id) is None
        assert s3.calls == []

    def test_never_raises_for_any_input(self, s3):
        """Contract: any garbage in -> None out, exception never escapes."""
        s3.objects["k"] = _make_block({"d": "正文"})
        for block_key in (None, "", 123, "k"):
            for doc_id in (None, "", 456, "d"):
                out = wb.fetch_judgment_body(block_key, doc_id)
                assert out is None or out == "正文"


def _make_legacy_block(docs: dict[str, str]) -> bytes:
    """Build a block in the HISTORICAL Windows-writer layout: u32-BE header
    byte length + plaintext JSON header + zstd-compressed payload with
    per-doc [absolute_start, byte_len] offset pairs (layout decoded from
    live bucket objects, 2026-09-29)."""
    payloads = [text.encode("utf-8") for text in docs.values()]
    offsets: list[list[int]] = []
    start = 0
    for buf in payloads:
        offsets.append([start, len(buf)])
        start += len(buf)
    header = {"docs": list(docs), "offsets": offsets,
              "raw": None, "src": "wenshu"}
    blob = json.dumps(header).encode("utf-8")
    return (len(blob).to_bytes(4, "big") + blob
            + zstandard.ZstdCompressor(level=19).compress(b"".join(payloads)))


class TestLegacyFormat:
    """The historical writer's objects — the only kind live in the bucket."""

    def test_hit_returns_exact_utf8_text(self, s3):
        s3.objects["blocks/2024/09/000711.zst"] = _make_legacy_block({
            "doc-a": "本院认为，合同合法有效。",
            "doc-b": "判决如下：驳回上诉，维持原判。",
        })
        assert wb.fetch_judgment_body(
            "blocks/2024/09/000711.zst", "doc-a") == "本院认为，合同合法有效。"

    def test_html_payload_served_as_stored(self, s3):
        html = "<!DOCTYPE HTML PUBLIC><html><body>裁判文书正文</body></html>"
        s3.objects["blocks/2024/09/000001.zst"] = _make_legacy_block(
            {"doc-h": html})
        assert wb.fetch_judgment_body(
            "blocks/2024/09/000001.zst", "doc-h") == html

    def test_doc_not_in_block_returns_none(self, s3):
        s3.objects["blocks/2025/10/000020.zst"] = _make_legacy_block(
            {"doc-a": "甲文"})
        assert wb.fetch_judgment_body(
            "blocks/2025/10/000020.zst", "doc-zz") is None

    def test_bad_header_json_returns_none(self, s3):
        blob = b"{not json"
        s3.objects["k"] = len(blob).to_bytes(4, "big") + blob + b"\x28\xb5\x2f\xfd"
        assert wb.fetch_judgment_body("k", "d") is None

    def test_implausible_header_length_returns_none(self, s3):
        s3.objects["k"] = (999_999_999).to_bytes(4, "big") + b"{}"
        assert wb.fetch_judgment_body("k", "d") is None

    def test_offsets_length_mismatch_returns_none(self, s3):
        blob = json.dumps({"docs": ["a", "b"], "offsets": [[0, 1]]}).encode()
        s3.objects["k"] = len(blob).to_bytes(4, "big") + blob + b"\x28\xb5\x2f\xfd"
        assert wb.fetch_judgment_body("k", "a") is None

    def test_malformed_pair_returns_none(self, s3):
        blob = json.dumps({"docs": ["a"], "offsets": [[5]]}).encode()
        s3.objects["k"] = len(blob).to_bytes(4, "big") + blob + b"\x28\xb5\x2f\xfd"
        assert wb.fetch_judgment_body("k", "a") is None

    def test_out_of_bounds_pair_returns_none(self, s3):
        blob = json.dumps({"docs": ["a"], "offsets": [[0, 99999]]}).encode()
        s3.objects["k"] = len(blob).to_bytes(4, "big") + blob + b"\x28\xb5\x2f\xfd"
        assert wb.fetch_judgment_body("k", "a") is None

    def test_bad_payload_zstd_returns_none(self, s3):
        blob = json.dumps({"docs": ["a"], "offsets": [[0, 4]]}).encode()
        s3.objects["k"] = len(blob).to_bytes(4, "big") + blob + b"garbage!!"
        assert wb.fetch_judgment_body("k", "a") is None


class TestClientConfig:
    def test_env_overrides_and_defaults(self, monkeypatch):
        monkeypatch.delenv("FDBIZ_RUSTFS_ENDPOINT", raising=False)
        monkeypatch.delenv("FDBIZ_RUSTFS_REGION", raising=False)
        monkeypatch.delenv("FDBIZ_RUSTFS_WENSHU_BUCKET", raising=False)
        assert wb._endpoint() == "http://100.64.0.11:9000"
        assert wb._region() == "us-east-1"
        assert wb.bucket() == "wenshu"
        monkeypatch.setenv("FDBIZ_RUSTFS_ENDPOINT", "http://127.0.0.1:9")
        monkeypatch.setenv("FDBIZ_RUSTFS_WENSHU_BUCKET", "wenshu-dev")
        assert wb._endpoint() == "http://127.0.0.1:9"
        assert wb.bucket() == "wenshu-dev"

    def test_client_builds_offline_and_is_cached(self, monkeypatch):
        monkeypatch.setenv("FDBIZ_RUSTFS_ACCESS_KEY", "ak")
        monkeypatch.setenv("FDBIZ_RUSTFS_SECRET_KEY", "sk")
        monkeypatch.delenv("FDBIZ_RUSTFS_ENDPOINT", raising=False)
        monkeypatch.delenv("FDBIZ_RUSTFS_REGION", raising=False)
        wb._client_cache.clear()
        try:
            c1 = wb._client()
            c2 = wb._client()
            assert c1 is c2  # same credential set -> cached client
        finally:
            wb._client_cache.clear()
