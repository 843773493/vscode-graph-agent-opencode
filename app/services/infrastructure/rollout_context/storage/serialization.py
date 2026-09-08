"""rollout checkpoint typed value 与 v2 format dispatch owner。

本模块只负责序列化端口、格式版本和 reasoning projection schema 检查；它不
构造 LangChain 对象，typed serializer 由 checkpoint 组装层注入。
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping

from app.services.infrastructure.rollout_context.migration.dispatch import (
    require_v2_runtime,
)
from app.services.infrastructure.rollout_context.storage import (
    schema as storage_version,
)


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_json_text(value: object) -> str:
    """以 RFC 8785 JCS 返回无空白 UTF-8 文本。"""
    from app.domain.itemized.hashing import canonical_json_bytes

    return canonical_json_bytes(value).decode("utf-8")


def canonical_json_line(value: object) -> bytes:
    """编码一条不可变 v2 JSONL record，并保留唯一换行符。"""
    from app.domain.itemized.hashing import canonical_json_bytes

    return canonical_json_bytes(value) + b"\n"


def _strict_non_negative_int(value: object, *, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError(f"{field} 必须是非负整数")
    return value


def _strict_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{field} 必须是非空字符串")
    return value


class RolloutSerializationMixin:
    """typed checkpoint value 与 v2 runtime dispatch 的唯一 owner。"""

    def _encode(self, value: object) -> tuple[str, bytes, int, str]:
        serde = self._serde
        if serde is None:
            raise RuntimeError(
                "RolloutStorage 未注入 SerializerPort；typed checkpoint value 只能由 checkpoint 组装层序列化"
            )
        serializer, blob = serde.dumps_typed(value)
        return serializer, blob, len(blob), _hash_bytes(blob)

    def _validate_reasoning_projection_schema(
        self,
        thread_id: str,
        checkpoint_ns: str,
    ) -> None:
        with self._connect(thread_id, checkpoint_ns, read_only=True) as connection:
            self._validate_reasoning_projection_connection(connection)

    @staticmethod
    def _validate_reasoning_projection_connection(
        connection: sqlite3.Connection,
    ) -> None:
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(reasoning_blocks)"
            ).fetchall()
        }
        if {"block_index", "block_kind"} & columns:
            raise RuntimeError(
                "rollout SQLite 使用已移除的 reasoning_blocks 旧 schema；"
                "原型阶段不提供兼容迁移，请重新生成该 rollout"
            )
        required = {
            "message_sequence",
            "content_block_index",
            "item_index",
            "carrier_type",
        }
        if not required.issubset(columns):
            raise RuntimeError(
                "rollout SQLite reasoning_blocks schema 不完整: "
                f"missing={sorted(required - columns)}"
            )

    def decode_value(self, value: Mapping[str, object] | tuple[str, bytes]) -> object:
        serde = self._serde
        if serde is None:
            raise RuntimeError(
                "RolloutStorage 未注入 SerializerPort；typed checkpoint value 只能由 checkpoint 组装层反序列化"
            )
        if isinstance(value, tuple):
            serializer, blob = value
        else:
            serializer = value.get("serializer_name")
            blob = value.get("value_blob")
        if not isinstance(serializer, str) or not isinstance(blob, (bytes, bytearray)):
            raise TypeError("SQLite serialized value 结构非法")
        return serde.loads_typed((serializer, bytes(blob)))

    @staticmethod
    def _rollout_format(connection: sqlite3.Connection) -> int:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(database_meta)")
        }
        if "rollout_format_version" not in columns:
            return 1
        row = connection.execute(
            "SELECT rollout_format_version FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("rollout database_meta 缺失")
        return _strict_non_negative_int(
            row[0], field="database_meta.rollout_format_version"
        )

    @classmethod
    def _require_v2_runtime(cls, connection: sqlite3.Connection) -> None:
        """禁止 v1 artifact 进入正常 runtime/history/provider/checkpoint。"""
        rollout_format = cls._rollout_format(connection)
        require_v2_runtime(
            rollout_format,
            expected=storage_version.ROLLOUT_FORMAT_VERSION,
        )

    @classmethod
    def _require_v2_readable(
        cls,
        connection: sqlite3.Connection,
        *,
        allow_migrating: bool = False,
    ) -> None:
        """拒绝读取尚未完成 legacy staging 的 target。"""
        cls._require_v2_runtime(connection)
        row = connection.execute(
            "SELECT database_state FROM database_meta WHERE singleton_id = 1"
        ).fetchone()
        if row is None:
            raise RuntimeError("rollout database_meta 缺失，无法判断读取状态")
        database_state = _strict_text(row[0], field="database_meta.database_state")
        if database_state == "migrating" and not allow_migrating:
            raise RuntimeError(
                "legacy migration target 在安装完成前不可读取；"
                "请等待 migration 完成后重新打开 rollout"
            )

    def supports_itemized_context(
        self,
        thread_id: str,
        *,
        checkpoint_ns: str = "",
    ) -> bool:
        """判断 rollout 是否允许 v2 item/context owner 参与 provider 请求。"""
        self.initialize(thread_id, checkpoint_ns)
        with self._connect(thread_id, checkpoint_ns, read_only=True) as connection:
            self._require_v2_runtime(connection)
            return True


__all__ = [
    "RolloutSerializationMixin",
    "canonical_json_line",
    "canonical_json_text",
]
