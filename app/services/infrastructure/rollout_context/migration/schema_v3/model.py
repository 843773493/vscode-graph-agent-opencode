"""显式 artifact 升级的数据与能力边界。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    DetailRecord,
)

if TYPE_CHECKING:
    from app.services.infrastructure.rollout_context.runtime.protected_upgrade import (
        ProtectedDetailUpgrade,
    )


class SchemaV3UpgradeError(RuntimeError):
    """旧 artifact 无法完整验证；不得切换到新 schema。"""


class SchemaV3DetailCapability(Protocol):
    """显式入口注入的迁移能力；实现方不向本模块暴露私有 cipher。"""

    def require_protected_key(self) -> None:
        """没有既有 protected key 时在任何发布前抛错。"""
        ...

    def prepare_legacy_detail(
        self, *, legacy_envelope: Mapping[str, object], legacy_blob: bytes,
        legacy_session_id: str, legacy_detail_id: str, target_ref: DetailRef,
        detail_kind: str, retention_class: str, visibility: str, required: bool,
        checkpoint_ns: str, expected_digest: str,
    ) -> ProtectedDetailUpgrade:
        """认证旧 AAD/正文/digest 后一次返回新 record/envelope/密文，不写文件。"""
        ...

    def verify_prepared_detail(
        self, *, record: DetailRecord, manifest_bytes: bytes, protected_bytes: bytes,
    ) -> None:
        """用现有 key 认证暂存 typed 密文与 manifest/AAD，不返回正文。"""
        ...


@dataclass(frozen=True)
class DetailUpgrade:
    old_id: str
    old_path: str
    old_raw: bytes | None
    record: DetailRecord
    new_raw: bytes | None
    created_at: str
    protected_old_path: str | None = None
    protected_old_raw: bytes | None = None
    protected_new_raw: bytes | None = None


def object_json(raw: str | bytes, *, field: str) -> dict[str, object]:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise SchemaV3UpgradeError(f"source-mismatch: {field} 重复 JSON key")
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise SchemaV3UpgradeError(f"source-mismatch: {field} 必须是 object")
    canonical_json_bytes(value)
    return value


def rows(connection: sqlite3.Connection, table: str) -> list[dict[str, object]]:
    # table 来自模块常量或 sqlite_master；仍转义名称，不能让 artifact 构造 SQL。
    quoted = table.replace('"', '""')
    cursor = connection.execute(f'SELECT * FROM "{quoted}"')
    columns = tuple(column[0] for column in cursor.description)
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def require_equal(actual: object, expected: object, field: str) -> None:
    if type(actual) is not type(expected) or actual != expected:
        raise SchemaV3UpgradeError(f"source-mismatch: {field} 不一致")
