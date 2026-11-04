"""旧 blob 只读身份校验与当前附件目录使用的显式 UTC 日期换算。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from app.services.infrastructure.attachment_blob_catalog.blob_identity import (
    digest_for_blob_id,
    utc_bucket_date,
    validate_blob_id,
    validate_blob_relative_locator,
    validate_digest,
)

_ZERO_HEX = "0" * 64


# ----------------------------------------------------------------------
# 身份形态：显式校验，非法即抛
# ----------------------------------------------------------------------


def test_blob_id_is_exactly_68_bytes_ascii() -> None:
    blob_id = "blb_" + _ZERO_HEX
    assert len(blob_id.encode("ascii")) == 68
    validate_blob_id(blob_id)


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


def test_digest_for_legacy_blob_id_preserves_frozen_payload() -> None:
    payload = "0123456789abcdef" * 4
    assert digest_for_blob_id("blb_" + payload) == "sha256:" + payload


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


def test_legacy_blob_locator_accepts_frozen_date_bucket() -> None:
    validate_blob_relative_locator("2026/01/09/blb_" + _ZERO_HEX)


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
