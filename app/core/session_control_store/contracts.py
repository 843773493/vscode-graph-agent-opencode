"""session-control.sqlite 行边界使用的稳定 GraphBinding 投影。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Final

_GRAPH_BINDING_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "graph_id",
        "graph_revision",
        "graph_schema_hash",
        "capability_profile_hash",
    }
)
_SHA256_DIGEST_PATTERN: Final[re.Pattern[str]] = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class ThreadGraphBindingRecord:
    """thread_catalog 中持久化的 GraphBinding 四字段只读投影。"""

    graph_id: str
    graph_revision: int
    graph_schema_hash: str
    capability_profile_hash: str

    def __post_init__(self) -> None:
        if not isinstance(self.graph_id, str) or not self.graph_id.strip():
            raise ValueError("graph_id 必须是非空字符串")
        if not isinstance(self.graph_revision, int) or isinstance(
            self.graph_revision, bool
        ):
            raise TypeError("graph_revision 必须是整数")
        if self.graph_revision < 1:
            raise ValueError("graph_revision 必须 >= 1")
        for field_name in ("graph_schema_hash", "capability_profile_hash"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not _SHA256_DIGEST_PATTERN.fullmatch(
                value
            ):
                raise ValueError(f"{field_name} 必须是 sha256 摘要: {value!r}")

    @classmethod
    def from_creation_json(cls, value: str) -> ThreadGraphBindingRecord:
        """从 child creation record 的冻结 JSON 提取精确四字段。"""
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "thread creation record 的 GraphBinding JSON 无法解析（fail closed）"
            ) from error
        if not isinstance(parsed, dict) or set(parsed) != _GRAPH_BINDING_FIELDS:
            raise RuntimeError(
                "thread creation record 的 GraphBinding 必须恰含四字段（fail closed）: "
                f"fields={sorted(parsed) if isinstance(parsed, dict) else type(parsed).__name__}"
            )
        try:
            return cls(
                graph_id=parsed["graph_id"],
                graph_revision=parsed["graph_revision"],
                graph_schema_hash=parsed["graph_schema_hash"],
                capability_profile_hash=parsed["capability_profile_hash"],
            )
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                "thread creation record 的 GraphBinding 字段非法（fail closed）"
            ) from error

    def as_tuple(self) -> tuple[str, int, str, str]:
        """按 thread_catalog 列顺序返回四字段。"""
        return (
            self.graph_id,
            self.graph_revision,
            self.graph_schema_hash,
            self.capability_profile_hash,
        )


__all__ = ["ThreadGraphBindingRecord"]
