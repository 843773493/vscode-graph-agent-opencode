"""附件摘要与 UTC 日期校验，以及离线迁移/冻结历史所需的旧 blob 只读校验。"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

__all__ = [
    "BLOB_ID_PATTERN",
    "DIGEST_PATTERN",
    "digest_for_blob_id",
    "utc_bucket_date",
    "validate_blob_id",
    "validate_blob_relative_locator",
    "validate_digest",
]

# blob 身份：``blb_`` + 64 位小写 hex，恰好 68 个 ASCII byte。
BLOB_ID_PATTERN = re.compile(r"blb_[0-9a-f]{64}")

# digest 固定形态 ``sha256:<64 位小写 hex>``。
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")

# 受检相对 locator：``YYYY/MM/DD/<blob-id>``（相对附件 store 根）。
_BLOB_RELATIVE_LOCATOR_PATTERN = re.compile(
    r"[0-9]{4}/[0-9]{2}/[0-9]{2}/blb_[0-9a-f]{64}"
)


def validate_blob_id(blob_id: str) -> None:
    """校验 blob-id 的固定 68-byte ASCII 形态。"""
    if not isinstance(blob_id, str):
        raise TypeError(f"blob_id 必须是字符串: {blob_id!r}")
    if BLOB_ID_PATTERN.fullmatch(blob_id) is None:
        raise ValueError(
            f"blob_id 形态非法（须为 blb_[0-9a-f]{{64}}）: {blob_id!r}"
        )


def digest_for_blob_id(blob_id: str) -> str:
    """由已校验 blob-id 还原 ``sha256:<hex>`` digest（同一 payload）。"""
    validate_blob_id(blob_id)
    return "sha256:" + blob_id[len("blb_"):]


def validate_digest(digest: str) -> None:
    """校验 digest 的固定 ``sha256:<64 位小写 hex>`` 形态。"""
    if not isinstance(digest, str):
        raise TypeError(f"digest 必须是字符串: {digest!r}")
    if DIGEST_PATTERN.fullmatch(digest) is None:
        raise ValueError(
            f"digest 形态非法（须为 sha256:<64 位小写 hex>）: {digest!r}"
        )


def utc_bucket_date(claimed_at: datetime) -> date:
    """把显式时间戳换算为 UTC 日期分桶值。

    日期分桶的日期来源只能是本函数：调用方必须显式传入带时区的
    :class:`datetime.datetime`（目录创建 intent 的冻结时刻），本函数一律换算
    到 UTC 后取 ``date()``。绝不读取文件 mtime、进程本地时区或 ``now()``，
    因此同一时间戳在任何机器上得到同一分桶。
    """
    if isinstance(claimed_at, bool) or not isinstance(claimed_at, datetime):
        raise TypeError(f"claimed_at 必须是 datetime: {claimed_at!r}")
    if claimed_at.tzinfo is None:
        raise ValueError(
            "claimed_at 必须带显式时区（不得依赖进程本地时区）: "
            f"{claimed_at!r}"
        )
    return claimed_at.astimezone(UTC).date()


def validate_blob_relative_locator(locator: str) -> None:
    """校验受检 relative locator ``YYYY/MM/DD/<blob-id>``。"""
    if not isinstance(locator, str):
        raise TypeError(f"blob relative locator 必须是字符串: {locator!r}")
    if _BLOB_RELATIVE_LOCATOR_PATTERN.fullmatch(locator) is None:
        raise ValueError(
            "blob relative locator 形态非法"
            "（须为 YYYY/MM/DD/blb_[0-9a-f]{64}）: "
            f"{locator!r}"
        )
