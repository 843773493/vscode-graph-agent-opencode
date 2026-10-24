"""workspace 附件 store 的受检 locator 解析（8.3 附件链）。

本模块是附件物理路径的唯一生产者与解析者：调用方只能传 catalog 中已冻结
的受检 relative locator（或由 :mod:`blob_identity` 从 blob-id + 首次 UTC
日期生成的 locator），**绝不接受调用方拼出的字符串路径**。staging
locator 与最终 blob locator 是两个互不重叠的命名空间：staging 位于
``<attachments>/.staging/<ingest-id>``，最终正文位于
``<attachments>/YYYY/MM/DD/<blob-id>``。目录资源使用
``<attachments>/YYYY/MM/DD/<attachment-id>``，日期来自冻结创建时间，
与不可变正文摘要独立；当前先供离线迁移使用。各命名空间都不允许扫盘枚举定位。

错误分类：``TypeError`` 类型错、``ValueError`` 形态非法。
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from pathlib import Path, PureWindowsPath

from app.core.session_catalog_store import validate_path_budget
from app.services.infrastructure.attachment_blob_catalog.blob_identity import (
    utc_bucket_date,
    validate_blob_id,
    validate_blob_relative_locator,
)

__all__ = [
    "STAGING_DIRECTORY_NAME",
    "attachment_directory_relative_locator",
    "fork_import_ingest_id",
    "ingest_staging_relative_locator",
    "resolve_attachment_directory",
    "resolve_blob_path",
    "resolve_captured_attachment",
    "resolve_staging_path",
    "validate_attachment_directory_relative_locator",
    "validate_ingest_staging_relative_locator",
]

# 受控内部 staging 目录名：直接位于附件 store 根下，与日期分桶互不重叠。
STAGING_DIRECTORY_NAME = ".staging"

# staging relative locator 形态：``.staging/ing_[0-9a-f]{32}``。
_STAGING_RELATIVE_LOCATOR_PATTERN = re.compile(r"\.staging/ing_[0-9a-f]{32}")
_ATTACHMENT_DIRECTORY_PATTERN = re.compile(r"([0-9]{4})/([0-9]{2})/([0-9]{2})/(att_[0-9a-f]{32})")


def attachment_directory_relative_locator(attachment_id: str, created_at: datetime) -> str:
    """目录身份与内容摘要无关；日期只取显式创建 intent 的 UTC 日期。"""
    if not isinstance(attachment_id, str):
        raise TypeError("attachment_id 必须是字符串")
    bucket = utc_bucket_date(created_at)
    locator = f"{bucket.year:04d}/{bucket.month:02d}/{bucket.day:02d}/{attachment_id}"
    validate_attachment_directory_relative_locator(locator)
    return locator


def validate_attachment_directory_relative_locator(locator: str) -> None:
    """校验目录 locator，不清洗路径、日期或旧附件 ID。"""
    if not isinstance(locator, str):
        raise TypeError("附件目录 locator 必须是字符串")
    match = _ATTACHMENT_DIRECTORY_PATTERN.fullmatch(locator)
    if match is None:
        raise ValueError("附件目录 locator 必须为 YYYY/MM/DD/att_<32位小写hex>")
    date(*(int(part) for part in match.groups()[:3]))


def resolve_attachment_directory(attachments_root: Path, relative_locator: str) -> Path:
    """按唯一 catalog 冻结的目录 locator 检查完整路径预算后定位。"""
    validate_attachment_directory_relative_locator(relative_locator)
    validate_path_budget(attachments_root, relative_locator)
    return _checked_descendant(attachments_root, tuple(relative_locator.split("/")))


def _checked_descendant(root: Path, parts: tuple[str, ...]) -> Path:
    validate_attachment_file_segments(parts)
    path = root
    if root.is_symlink():
        raise RuntimeError("附件目录根不能是符号链接")
    for part in parts:
        path /= part
        if path.is_symlink():
            raise RuntimeError("附件目录及文件不能经过符号链接")
    return path


def validate_attachment_file_segments(parts: tuple[str, ...]) -> None:
    """冻结 manifest 和物理解析共用路径段校验，不做文件系统访问。"""
    for part in parts:
        if (not isinstance(part, str) or not part or part in {".", ".."}
                or "/" in part or "\\" in part or PureWindowsPath(part).drive
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in part)):
            raise ValueError("附件文件必须是受检目录内的原生单路径段")


def resolve_attachment_file(directory: Path, parts: tuple[str, ...]) -> Path:
    """读取、派生写入和冻结复制共用同一 containment 边界。"""
    validate_path_budget(directory, "/".join(parts))
    return _checked_descendant(directory, parts)


def resolve_captured_directory_file(captured_root: Path, attachment_id: str, relative_path: str) -> Path:
    """目录多文件快照进入原 capture 的文件树，不另建网络读取入口。"""
    return resolve_attachment_file(resolve_captured_directory(captured_root, attachment_id), tuple(relative_path.split("/")))


def resolve_captured_directory(captured_root: Path, attachment_id: str) -> Path:
    """捕获目录根由 locator 单点定位，调用方不从某个文件倒推布局。"""
    validate_attachment_directory_relative_locator("2000/01/01/" + attachment_id)
    parts = ("attachment-files", attachment_id)
    validate_path_budget(captured_root, "/".join(parts))
    return _checked_descendant(captured_root, parts)


def ingest_staging_relative_locator(ingest_id: str) -> str:
    """由软件生成的 ingest id 派生受控 staging relative locator。

    这是 staging locator 的唯一生产者；调用方不得自行拼接 staging 路径。
    """
    if not isinstance(ingest_id, str):
        raise TypeError(f"ingest_id 必须是字符串: {ingest_id!r}")
    locator = f"{STAGING_DIRECTORY_NAME}/{ingest_id}"
    validate_ingest_staging_relative_locator(locator)
    return locator


def validate_ingest_staging_relative_locator(locator: str) -> None:
    """校验受控 staging relative locator 的固定形态。"""
    if not isinstance(locator, str):
        raise TypeError(f"staging relative locator 必须是字符串: {locator!r}")
    if _STAGING_RELATIVE_LOCATOR_PATTERN.fullmatch(locator) is None:
        raise ValueError(
            "staging relative locator 形态非法"
            "（须为 .staging/ing_[0-9a-f]{32}）: "
            f"{locator!r}"
        )


def resolve_blob_path(attachments_root: Path, relative_locator: str) -> Path:
    """把已冻结的受检 blob locator 解析为绝对路径。

    先过形态校验与完整路径预算，再拼接；不做任何清洗、截断或大小写归一。
    """
    if not isinstance(attachments_root, Path):
        raise TypeError(f"attachments_root 必须是 Path: {attachments_root!r}")
    validate_blob_relative_locator(relative_locator)
    validate_path_budget(attachments_root, relative_locator)
    return attachments_root / relative_locator


def resolve_staging_path(attachments_root: Path, relative_locator: str) -> Path:
    """把 record 冻结的受控 staging locator 解析为绝对路径。"""
    if not isinstance(attachments_root, Path):
        raise TypeError(f"attachments_root 必须是 Path: {attachments_root!r}")
    validate_ingest_staging_relative_locator(relative_locator)
    validate_path_budget(attachments_root, relative_locator)
    return attachments_root / relative_locator


def fork_import_ingest_id(copy_operation_id: str, blob_id: str) -> str:
    """导入与清理共用原 operation/blob preimage，不改写已有 staging 身份。"""
    return "ing_" + hashlib.sha256(f"fork-import:{copy_operation_id}:{blob_id}".encode()).hexdigest()[:32]


def resolve_captured_attachment(captured_root: Path, blob_id: str) -> Path:
    """附件冻结文件属于原 capture，不再从 live blob 提供跨域分块。"""
    validate_blob_id(blob_id)
    locator = "attachment-files/" + blob_id
    validate_path_budget(captured_root, locator)
    path = captured_root / locator
    if any(candidate.is_symlink() for candidate in (captured_root.parent, captured_root, path.parent, path)):
        raise RuntimeError("fork 附件冻结文件不能经过符号链接")
    return path
