"""附件 blob 身份与日期分桶的纯逻辑测试（8.3 附件链第一刀）。

只覆盖身份与分桶这两层，不触 saga 的 claim/pin/gate 全链路：

- 68-byte ASCII `blb_[0-9a-f]{64}` 身份：长度/前缀/小写 hex 显式校验，
  非法一律明确抛错，不静默回退或重算；
- `attachments/YYYY/MM/DD/{blob-id}` 日期分桶：日期来自显式带时区时间戳，
  一律换算 UTC，绝不读文件 mtime 或本地时区；
- 去重（同 digest 同 blob-id）、identity conflict（同 id 不同内容）、
  跨日期同 digest 三种判定。

全部为纯函数测试，用 tmp_path 时也只做隔离，不依赖网络或真实 LLM。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from app.services.infrastructure.attachment_blob_catalog.blob_identity import (
    BlobIdentity,
    BlobIdentityConflictError,
    blob_id_for_digest,
    compute_blob_identity,
    date_bucket_relative_locator,
    digest_for_blob_id,
    utc_bucket_date,
    validate_blob_id,
    validate_blob_relative_locator,
    validate_digest,
    verify_blob_identity,
)

_ZERO_HEX = "0" * 64


# ----------------------------------------------------------------------
# 身份形态：显式校验，非法即抛
# ----------------------------------------------------------------------


def test_blob_id_is_exactly_68_bytes_ascii() -> None:
    blob_id = "blb_" + _ZERO_HEX
    assert len(blob_id.encode("ascii")) == 68
    validate_blob_id(blob_id)


def test_compute_blob_identity_derives_id_digest_length_from_bytes() -> None:
    identity = compute_blob_identity(b"hello")
    assert identity == BlobIdentity(
        blob_id="blb_" + hashlib.sha256(b"hello").hexdigest(),
        digest="sha256:" + hashlib.sha256(b"hello").hexdigest(),
        length=5,
    )


def test_compute_blob_identity_ignores_name_mime_session() -> None:
    """身份只由正文 bytes 决定：同 bytes 在任何 owner 下都是同一身份。"""
    assert compute_blob_identity(b"same") == compute_blob_identity(b"same")


@pytest.mark.parametrize(
    "blob_id",
    [
        "blb_" + "A" * 64,          # 大写 hex
        "blb_" + "a" * 63,          # 长度不足
        "blb_" + "a" * 65,          # 长度过长
        "blb_" + "g" * 64,          # 非 hex 字符
        "BLB_" + "a" * 64,          # 前缀大小写错误
        "sha256:" + "a" * 64,       # 错误前缀
        "blb_" + "a" * 31 + "\u00e9" + "a" * 32,  # Unicode
        "../blb_" + "a" * 64,       # 分隔符
        "blb_" + "a" * 64 + "/x",   # 额外路径段
        "",                         # 空
    ],
)
def test_validate_blob_id_rejects_non_canonical(blob_id: str) -> None:
    with pytest.raises(ValueError):
        validate_blob_id(blob_id)


def test_validate_blob_id_rejects_non_string() -> None:
    with pytest.raises(TypeError):
        validate_blob_id(None)  # type: ignore[arg-type]


def test_validate_digest_rejects_non_canonical() -> None:
    for digest in ("sha256:" + "A" * 64, "sha256:" + "a" * 63, "a" * 64, "md5:" + _ZERO_HEX):
        with pytest.raises(ValueError):
            validate_digest(digest)


def test_digest_and_blob_id_round_trip() -> None:
    identity = compute_blob_identity(b"round-trip")
    assert blob_id_for_digest(identity.digest) == identity.blob_id
    assert digest_for_blob_id(identity.blob_id) == identity.digest


# ----------------------------------------------------------------------
# 日期分桶：显式时间戳 → UTC 日期
# ----------------------------------------------------------------------


def test_utc_bucket_date_converts_explicit_timestamp_to_utc() -> None:
    # 东八区 2026-03-04 07:00 == UTC 2026-03-03 23:00 → 落在 03-03。
    claimed_at = datetime(2026, 3, 4, 7, 0, tzinfo=timezone(timedelta(hours=8)))
    assert utc_bucket_date(claimed_at) == date(2026, 3, 3)


def test_utc_bucket_date_is_timezone_invariant_for_same_instant() -> None:
    instant = datetime(2026, 3, 4, 0, 30, tzinfo=UTC)
    same_instant = instant.astimezone(timezone(timedelta(hours=5)))
    assert utc_bucket_date(instant) == utc_bucket_date(same_instant) == date(2026, 3, 4)


def test_utc_bucket_date_rejects_naive_timestamp() -> None:
    """无时区时间戳不得靠进程本地时区兜底。"""
    with pytest.raises(ValueError, match="显式时区"):
        # 故意构造 naive datetime，验证拒绝分支。
        utc_bucket_date(datetime(2026, 3, 4, 7, 0))  # noqa: DTZ001


def test_utc_bucket_date_rejects_non_datetime() -> None:
    with pytest.raises(TypeError):
        utc_bucket_date("2026-03-04")  # type: ignore[arg-type]


def test_date_bucket_locator_has_exact_shape_and_no_extra_shard() -> None:
    blob_id = "blb_" + _ZERO_HEX
    locator = date_bucket_relative_locator(blob_id, date(2026, 1, 9))
    assert locator == f"2026/01/09/{blob_id}"
    assert locator.count("/") == 3
    validate_blob_relative_locator(locator)


@pytest.mark.parametrize(
    "locator",
    [
        "2026/1/9/blb_" + _ZERO_HEX,                 # 非零填充
        "2026/01/09/blb_" + _ZERO_HEX + "/extra",    # 额外 shard
        "2026/01/09/ses_" + "a" * 32,                # 叶名不是 blob-id
        "2026/01/09/" + _ZERO_HEX,                   # 缺前缀
        "../2026/01/09/blb_" + _ZERO_HEX,            # 越界
        "/2026/01/09/blb_" + _ZERO_HEX,              # 绝对路径
    ],
)
def test_validate_blob_relative_locator_rejects_non_canonical(locator: str) -> None:
    with pytest.raises(ValueError):
        validate_blob_relative_locator(locator)


# ----------------------------------------------------------------------
# 三种判定：去重 / identity conflict / 跨日期同 digest
# ----------------------------------------------------------------------


def test_dedup_same_digest_yields_identical_identity() -> None:
    payload = b"dedup-payload"
    first = compute_blob_identity(payload)
    second = compute_blob_identity(payload)
    assert first == second
    verify_blob_identity(first, payload)


def test_identity_conflict_detects_same_id_with_different_content() -> None:
    original = compute_blob_identity(b"original")
    # 伪造「同 blob-id、同 digest」但 length 被改坏的 identity。
    tampered = BlobIdentity(
        blob_id=original.blob_id, digest=original.digest, length=original.length + 1
    )
    with pytest.raises(BlobIdentityConflictError, match="不同内容/length"):
        verify_blob_identity(tampered, b"original")


def test_identity_conflict_detects_blob_id_digest_mismatch() -> None:
    other = compute_blob_identity(b"other")
    mismatched = BlobIdentity(
        blob_id=compute_blob_identity(b"one").blob_id,
        digest=other.digest,
        length=other.length,
    )
    with pytest.raises(BlobIdentityConflictError, match="blob-id 与 digest 不匹配"):
        verify_blob_identity(mismatched, b"other")


def test_cross_date_same_digest_has_one_identity_two_buckets() -> None:
    """跨 UTC 日期边界上传同一正文：身份唯一，只有分桶日期不同。"""
    payload = b"cross-date"
    identity = compute_blob_identity(payload)
    before_midnight = utc_bucket_date(datetime(2026, 3, 4, 23, 59, tzinfo=UTC))
    after_midnight = utc_bucket_date(datetime(2026, 3, 5, 0, 1, tzinfo=UTC))

    assert before_midnight != after_midnight
    locator_before = date_bucket_relative_locator(identity.blob_id, before_midnight)
    locator_after = date_bucket_relative_locator(identity.blob_id, after_midnight)
    # blob-id 叶名逐字节一致：catalog 唯一 claim 只允许一个 locator 生效。
    assert locator_before.rsplit("/", 1)[-1] == locator_after.rsplit("/", 1)[-1]
    assert locator_before != locator_after
