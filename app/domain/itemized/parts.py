"""Canonical item payload 内的 content-part 与 durable anchor 值对象。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import _ensure_json_value, sha256_jcs
from app.domain.itemized.schema import SEMANTIC_KINDS


def _non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ItemSchemaError(f"{field_name} 必须是非空字符串")
    return value


@dataclass(frozen=True, slots=True)
class ContentPart:
    """父 item payload 内的稳定 part；正文不在 SQLite 复制。"""

    part_id: str
    part_ordinal: int
    part_semantic_kind: str
    content: object
    content_hash: str
    prefix_hash: str | None = None

    def __post_init__(self) -> None:
        _non_empty_string(self.part_id, "ContentPart.part_id")
        if (
            not isinstance(self.part_ordinal, int)
            or isinstance(self.part_ordinal, bool)
            or self.part_ordinal < 0
        ):
            raise ItemSchemaError("content part ordinal 不能为负数")
        _non_empty_string(self.part_semantic_kind, "ContentPart.part_semantic_kind")
        if self.part_semantic_kind not in SEMANTIC_KINDS:
            raise ItemSchemaError(
                f"未知 content part semantic kind: {self.part_semantic_kind}"
            )
        if self.content_hash != sha256_jcs(self.content):
            raise ItemSchemaError("content part hash 与正文不一致")
        if self.prefix_hash is not None:
            _non_empty_string(self.prefix_hash, "ContentPart.prefix_hash")

    @classmethod
    def create(
        cls,
        *,
        part_id: str,
        part_ordinal: int,
        part_semantic_kind: str,
        content: object,
        prefix: object | None = None,
    ) -> ContentPart:
        if part_ordinal < 0:
            raise ItemSchemaError("content part ordinal 不能为负数")
        _non_empty_string(part_id, "part_id")
        _non_empty_string(part_semantic_kind, "part_semantic_kind")
        return cls(
            part_id=part_id,
            part_ordinal=part_ordinal,
            part_semantic_kind=part_semantic_kind,
            content=content,
            content_hash=sha256_jcs(content),
            prefix_hash=sha256_jcs(prefix) if prefix is not None else None,
        )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "part_id": self.part_id,
            "part_ordinal": self.part_ordinal,
            "part_semantic_kind": self.part_semantic_kind,
            "content": self.content,
            "content_hash": self.content_hash,
        }
        if self.prefix_hash is not None:
            result["prefix_hash"] = self.prefix_hash
        return result


@dataclass(frozen=True, slots=True)
class ContentPartAnchor:
    """durable part anchor；未声明 recovery capability 的 fragment 不可操作。"""

    anchor_id: str
    item_id: str
    part_id: str
    mode: str
    view_id: str
    branch_id: str
    capability: str
    content_hash: str
    prefix_hash: str | None = None
    fragment_identity: str | None = None
    fragment_length: int | None = None
    fragment_hash: str | None = None
    fragment_layout: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        for name in (
            "anchor_id",
            "item_id",
            "part_id",
            "view_id",
            "branch_id",
            "capability",
            "content_hash",
        ):
            _non_empty_string(getattr(self, name), name)
        if self.mode not in {"before", "inclusive"}:
            raise ItemSchemaError("content part anchor mode 必须是 before/inclusive")
        if self.capability not in {"content_part", "fragment"}:
            raise ItemSchemaError("未知 content part anchor capability")
        if self.prefix_hash is not None:
            _non_empty_string(self.prefix_hash, "prefix_hash")
        if self.capability == "fragment":
            _non_empty_string(self.fragment_identity, "fragment_identity")
            if (
                not isinstance(self.fragment_length, int)
                or isinstance(self.fragment_length, bool)
                or self.fragment_length < 0
            ):
                raise ItemSchemaError("fragment anchor 必须包含非负 fragment_length")
            _non_empty_string(self.fragment_hash, "fragment_hash")
            if not isinstance(self.fragment_layout, Mapping) or not self.fragment_layout:
                raise ItemSchemaError("fragment anchor 必须包含可恢复的 fragment_layout")
            _ensure_json_value(self.fragment_layout, "fragment_layout")
        elif any(
            value is not None
            for value in (
                self.fragment_identity,
                self.fragment_length,
                self.fragment_hash,
                self.fragment_layout,
            )
        ):
            raise ItemSchemaError("content_part anchor 不得携带 fragment 专用字段")


__all__ = ["ContentPart", "ContentPartAnchor"]
