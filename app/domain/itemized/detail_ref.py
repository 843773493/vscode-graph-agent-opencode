"""受 owner 限定的逻辑详情引用；不是路径，也不含第二个物理 identity。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.domain.itemized.errors import ItemSchemaError


@dataclass(frozen=True, slots=True)
class DetailRef:
    session_id: str
    assembly_id: str
    detail_id: str

    def __post_init__(self) -> None:
        for name in ("session_id", "assembly_id", "detail_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or "\x00" in value:
                raise ItemSchemaError(f"DetailRef.{name} 必须是非空 identity")
        for name in ("assembly_id", "detail_id"):
            value = getattr(self, name)
            if value in {".", ".."} or "/" in value or "\\" in value:
                raise ItemSchemaError(f"DetailRef.{name} 必须是安全的单一 identity 段")

    def to_dict(self) -> dict[str, str]:
        return {
            "session_id": self.session_id,
            "assembly_id": self.assembly_id,
            "detail_id": self.detail_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> DetailRef:
        if not isinstance(value, Mapping) or set(value) != {
            "session_id", "assembly_id", "detail_id",
        }:
            raise ItemSchemaError("detail_ref 必须是完整且无额外字段的 typed owner reference")
        return cls(
            session_id=value["session_id"],
            assembly_id=value["assembly_id"],
            detail_id=value["detail_id"],
        )

    def require_owner(self, session_id: str, assembly_id: str | None = None) -> None:
        if self.session_id != session_id or (
            assembly_id is not None and self.assembly_id != assembly_id
        ):
            raise ItemSchemaError("source-mismatch: detail_ref 不属于请求的 session/assembly")


__all__ = ["DetailRef"]
