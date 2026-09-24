"""workspace 附件 content-addressed blob 的身份与受检相对 locator（8.3 附件链）。

固定合同（design.md 820-822 / rollout-checkpoint-storage spec）：

- ``blob-id`` 是 68-byte ASCII ``blb_[0-9a-f]{64}``，payload 精确等于正文
  bytes 的 SHA-256 小写 hex；与 ``digest=sha256:<同一 hex>`` 及 length 逐字节
  一致，不依赖原始文件名、扩展名、MIME、Session 或 thread。
- 物理分桶唯一形态为 ``<.boxteam>/attachments/YYYY/MM/DD/<blob-id>``；日期是
  该 digest 首次成功取得 catalog 唯一 claim 的 UTC 日期，日期与叶名之间不
  增加 Session/thread/digest shard。
- resolver 只接受本模块产出的受检 relative locator；禁止把调用方字符串拼成
  路径（调用方只能传 ``blob_id``/``digest``，绝不传路径）。

日期分桶的日期来源必须是**显式时间戳**：由 :func:`utc_bucket_date` 从一个
带时区的 :class:`datetime.datetime` 换算为 UTC 日期，绝不读取文件 mtime、
进程本地时区或当前时间。

错误分类：``TypeError`` 类型错、``ValueError`` 形态非法、
``BlobIdentityConflictError`` 身份三元组内部矛盾。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

__all__ = [
    "ATTACHMENT_ROOT_DIRECTORY_NAME",
    "BLOB_ID_PATTERN",
    "DIGEST_PATTERN",
    "BlobIdentity",
    "BlobIdentityConflictError",
    "blob_id_for_digest",
    "compute_blob_identity",
    "date_bucket_relative_locator",
    "digest_for_blob_id",
    "utc_bucket_date",
    "validate_blob_id",
    "validate_blob_relative_locator",
    "validate_digest",
    "verify_blob_identity",
]

# ``<.boxteam>`` 下的附件根目录名（workspace 级内容寻址 store 的唯一根）。
ATTACHMENT_ROOT_DIRECTORY_NAME = "attachments"

# blob 身份：``blb_`` + 64 位小写 hex，恰好 68 个 ASCII byte。
BLOB_ID_PATTERN = re.compile(r"blb_[0-9a-f]{64}")

# digest 固定形态 ``sha256:<64 位小写 hex>``。
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")

# 受检相对 locator：``YYYY/MM/DD/<blob-id>``（相对附件 store 根）。
_BLOB_RELATIVE_LOCATOR_PATTERN = re.compile(
    r"[0-9]{4}/[0-9]{2}/[0-9]{2}/blb_[0-9a-f]{64}"
)


@dataclass(frozen=True, slots=True)
class BlobIdentity:
    """一段精确正文 bytes 的 blob 身份（digest/blob_id/length 三元组）。"""

    blob_id: str
    digest: str
    length: int


class BlobIdentityConflictError(RuntimeError):
    """identity 三元组内部矛盾（同一 blob-id 对应不同 digest/length）。

    这是附件链唯一的身份冲突错误类型：catalog 写入与纯身份校验共用同一
    实现，任何「同 id 不同内容」都必须抛本错误，绝不覆盖或静默重算。
    """


def compute_blob_identity(data: bytes) -> BlobIdentity:
    """由精确正文 bytes 计算身份；不读取文件名/MIME/Session。"""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(f"blob 正文必须是 bytes: {type(data).__name__}")
    payload = hashlib.sha256(bytes(data)).hexdigest()
    return BlobIdentity(
        blob_id=f"blb_{payload}",
        digest=f"sha256:{payload}",
        length=len(data),
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


def blob_id_for_digest(digest: str) -> str:
    """由已校验 digest 还原 blob-id（同一 payload）。"""
    validate_digest(digest)
    return "blb_" + digest[len("sha256:"):]


def validate_digest(digest: str) -> None:
    """校验 digest 的固定 ``sha256:<64 位小写 hex>`` 形态。"""
    if not isinstance(digest, str):
        raise TypeError(f"digest 必须是字符串: {digest!r}")
    if DIGEST_PATTERN.fullmatch(digest) is None:
        raise ValueError(
            f"digest 形态非法（须为 sha256:<64 位小写 hex>）: {digest!r}"
        )


def date_bucket_relative_locator(blob_id: str, bucket_date: date) -> str:
    """由 blob-id 与首次 claim 的 UTC 日期拼出受检 relative locator。

    ``bucket_date`` 必须是 :class:`datetime.date`；本函数是相对 locator 的
    唯一生产者，调用方不得自行拼接路径。
    """
    validate_blob_id(blob_id)
    if isinstance(bucket_date, bool) or not isinstance(bucket_date, date):
        raise TypeError(f"bucket_date 必须是 datetime.date: {bucket_date!r}")
    return (
        f"{bucket_date.year:04d}/{bucket_date.month:02d}/"
        f"{bucket_date.day:02d}/{blob_id}"
    )


def utc_bucket_date(claimed_at: datetime) -> date:
    """把显式时间戳换算为 UTC 日期分桶值。

    日期分桶的日期来源只能是本函数：调用方必须显式传入带时区的
    :class:`datetime.datetime`（首次取得唯一 claim 的时刻），本函数一律换算
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


def verify_blob_identity(identity: BlobIdentity, data: bytes) -> None:
    """校验已冻结的 identity 三元组与给定正文逐字节一致。

    用于去重与 identity conflict 判定：同 digest 同 blob-id 才可复用；同
    blob-id 但 digest/length/正文任一不一致时抛
    :class:`BlobIdentityConflictError`，绝不覆盖既有 blob。
    """
    if not isinstance(identity, BlobIdentity):
        raise TypeError(f"identity 必须是 BlobIdentity: {identity!r}")
    validate_blob_id(identity.blob_id)
    validate_digest(identity.digest)
    if blob_id_for_digest(identity.digest) != identity.blob_id:
        raise BlobIdentityConflictError(
            "identity 三元组的 blob-id 与 digest 不匹配"
            "（同一 blob-id 对应不同 digest）: "
            f"blob_id={identity.blob_id!r}, digest={identity.digest!r}"
        )
    actual = compute_blob_identity(data)
    if actual.blob_id != identity.blob_id or actual.length != identity.length:
        raise BlobIdentityConflictError(
            "identity 三元组与正文不符（同一 blob-id 对应不同内容/length）: "
            f"blob_id={identity.blob_id!r}, expected_length={identity.length}, "
            f"actual_blob_id={actual.blob_id!r}, actual_length={actual.length}"
        )


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
