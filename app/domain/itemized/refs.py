"""v2 ContextRef 与 ToolSetRef 的领域定义。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.enums import (
    BaseDeltaRole,
    CanonicalItemStatus,
    DetailAvailability,
    DetailProtection,
    PayloadKind,
    SemanticKind,
)
from app.domain.itemized.errors import ItemSchemaError
from app.domain.itemized.hashing import (
    canonical_json_bytes,
    payload_content_length,
    sha256_jcs,
    validate_hash_token,
)
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.schema import validate_item_compatibility


def _non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ItemSchemaError(f"{field_name} 必须是非空字符串")
    return value


def ref_identity(ref: object) -> tuple[str, str]:
    ref_type = getattr(ref, "ref_type", None)
    ref_id = getattr(ref, "ref_id", None)
    if not isinstance(ref_type, str) or not ref_type:
        raise ItemSchemaError("context ref 缺少非空 ref_type")
    if not isinstance(ref_id, str) or not ref_id:
        raise ItemSchemaError("context ref 缺少非空 ref_id")
    return ref_type, ref_id


def require_manifest_token(ref: object) -> str:
    content_hash = getattr(ref, "content_hash", None)
    redacted_digest = getattr(ref, "redacted_stable_digest", None)
    if (content_hash is None) == (redacted_digest is None):
        raise ItemSchemaError(
            "context ref 必须恰好携带 content_hash 或 redacted_stable_digest"
        )
    token = content_hash if content_hash is not None else redacted_digest
    return validate_hash_token(
        token,
        "context ref manifest token",
        redacted=redacted_digest is not None,
    )


def unique_ref_identities(refs: object) -> tuple[object, ...]:
    result = tuple(refs)
    identities = [ref_identity(ref) for ref in result]
    if len(identities) != len(set(identities)):
        raise ItemSchemaError("context ref registry 存在重复 ref_type/ref_id")
    return result


def _tool_identity(value: Mapping[str, object]) -> str:
    """返回工具 manifest 的显式稳定 identity。"""
    for field_name in ("tool_id", "id"):
        candidate = value.get(field_name)
        if isinstance(candidate, str) and candidate:
            return candidate
    function = value.get("function")
    if isinstance(function, Mapping):
        candidate = function.get("name")
        if isinstance(candidate, str) and candidate:
            return candidate
    candidate = value.get("name")
    if isinstance(candidate, str) and candidate:
        return candidate
    return ""


@dataclass(frozen=True, slots=True, init=False)
class ContextRef:
    """Context selection 的非工具 tagged-union 成员。

    ``ref_type`` 是唯一的判别字段；request-only 身份只能由
    ``ref_type=request_only`` 表达，不能再通过第二个 bool 或旧别名表达。
    """

    ref_type: str
    ref_id: str
    session_id: str
    plan_id: str | None
    semantic_kind: str | None
    payload_kind: str | None
    status: str | None
    item_sequence: int | None
    source_revision: str | None
    content_length: int | None
    content_hash: str | None
    redacted_stable_digest: str | None
    source_ref: str | DetailRef | None
    base_delta_role: str
    source_overlay_epoch: int | None
    overlay_from_revision: str | None
    overlay_to_revision: str | None
    overlay_diff_hash: str | None
    visibility: str
    protection: str
    availability: str

    def __init__(
        self,
        ref_type: str,
        ref_id: str = "",
        semantic_kind: str | None = None,
        payload_kind: str | None = None,
        status: str | None = None,
        content_hash: str | None = None,
        source_revision: str | None = None,
        *,
        session_id: str,
        plan_id: str | None = None,
        item_sequence: int | None = None,
        content_length: int | None = None,
        redacted_stable_digest: str | None = None,
        source_ref: str | DetailRef | None = None,
        base_delta_role: str = "none",
        source_overlay_epoch: int | None = None,
        overlay_from_revision: str | None = None,
        overlay_to_revision: str | None = None,
        overlay_diff_hash: str | None = None,
        visibility: str = "internal",
        protection: str = "public",
        availability: str = "available",
    ) -> None:
        object.__setattr__(self, "ref_type", ref_type)
        object.__setattr__(self, "ref_id", ref_id)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "plan_id", plan_id)
        object.__setattr__(self, "semantic_kind", semantic_kind)
        object.__setattr__(self, "payload_kind", payload_kind)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "item_sequence", item_sequence)
        object.__setattr__(self, "source_revision", source_revision)
        object.__setattr__(self, "content_length", content_length)
        object.__setattr__(self, "content_hash", content_hash)
        object.__setattr__(self, "redacted_stable_digest", redacted_stable_digest)
        object.__setattr__(self, "source_ref", source_ref)
        object.__setattr__(self, "base_delta_role", base_delta_role)
        object.__setattr__(self, "source_overlay_epoch", source_overlay_epoch)
        object.__setattr__(self, "overlay_from_revision", overlay_from_revision)
        object.__setattr__(self, "overlay_to_revision", overlay_to_revision)
        object.__setattr__(self, "overlay_diff_hash", overlay_diff_hash)
        object.__setattr__(self, "visibility", visibility)
        object.__setattr__(self, "protection", protection)
        object.__setattr__(self, "availability", availability)
        self.__post_init__()

    def __post_init__(self) -> None:
        _non_empty_string(self.ref_type, "ContextRef.ref_type")
        _non_empty_string(self.ref_id, "ContextRef.ref_id")
        _non_empty_string(self.session_id, "ContextRef.session_id")
        if self.ref_type not in {"canonical_item", "request_only"}:
            raise ItemSchemaError(f"未知 ContextRef.ref_type: {self.ref_type}")
        if self.ref_type == "request_only":
            _non_empty_string(self.plan_id, "ContextRef.plan_id")
        elif self.plan_id is not None:
            raise ItemSchemaError("canonical ContextRef 不得携带 plan_id")
        if not isinstance(self.base_delta_role, str) or self.base_delta_role not in {
            item.value for item in BaseDeltaRole
        }:
            raise ItemSchemaError(
                f"未知 ContextRef.base_delta_role: {self.base_delta_role}"
            )
        if self.source_overlay_epoch is not None and (
            not isinstance(self.source_overlay_epoch, int)
            or isinstance(self.source_overlay_epoch, bool)
            or self.source_overlay_epoch < 0
        ):
            raise ItemSchemaError("ContextRef.source_overlay_epoch 必须是非负整数")
        if not isinstance(self.visibility, str) or self.visibility not in {
            "public", "internal", "private"
        }:
            raise ItemSchemaError(f"未知 ContextRef.visibility: {self.visibility}")
        if not isinstance(self.protection, str) or self.protection not in {
            "public", "redacted", "protected"
        }:
            raise ItemSchemaError(f"未知 ContextRef.protection: {self.protection}")
        if not isinstance(self.availability, str) or self.availability not in {
            "available",
            "unavailable",
            "forbidden",
            "expired",
        }:
            raise ItemSchemaError(f"未知 ContextRef.availability: {self.availability}")
        if self.content_length is not None and (
            not isinstance(self.content_length, int)
            or isinstance(self.content_length, bool)
            or self.content_length < 0
        ):
            raise ItemSchemaError("ContextRef.content_length 必须是非负整数")
        if self.content_hash is not None:
            _non_empty_string(self.content_hash, "ContextRef.content_hash")
        if self.redacted_stable_digest is not None:
            _non_empty_string(
                self.redacted_stable_digest,
                "ContextRef.redacted_stable_digest",
            )
            if self.protection == DetailProtection.PUBLIC:
                raise ItemSchemaError(
                    "ContextRef.redacted_stable_digest 不得用于 public protection"
                )
        if self.semantic_kind is not None:
            _non_empty_string(self.semantic_kind, "ContextRef.semantic_kind")
            if self.semantic_kind not in {item.value for item in SemanticKind}:
                raise ItemSchemaError(
                    f"未知 ContextRef.semantic_kind: {self.semantic_kind}"
                )
        if self.payload_kind is not None:
            _non_empty_string(self.payload_kind, "ContextRef.payload_kind")
            if self.payload_kind not in {item.value for item in PayloadKind}:
                raise ItemSchemaError(
                    f"未知 ContextRef.payload_kind: {self.payload_kind}"
                )
        if self.status is not None:
            _non_empty_string(self.status, "ContextRef.status")
            if self.status not in {item.value for item in CanonicalItemStatus}:
                raise ItemSchemaError(f"未知 ContextRef.status: {self.status}")
        if (
            self.availability == "available"
            or self.content_hash is not None
            or self.redacted_stable_digest is not None
        ):
            require_manifest_token(self)
        if self.ref_type == "canonical_item":
            if self.availability == "available":
                for name in ("semantic_kind", "payload_kind", "status", "source_revision"):
                    _non_empty_string(getattr(self, name), f"ContextRef.{name}")
                if self.content_length is None:
                    raise ItemSchemaError("canonical ContextRef 必须带 content_length")
            elif self.source_revision is not None:
                _non_empty_string(self.source_revision, "ContextRef.source_revision")
            if all(value is not None for value in (self.semantic_kind, self.payload_kind, self.status)):
                validate_item_compatibility(self.semantic_kind, self.payload_kind, self.status)
            if self.redacted_stable_digest is not None:
                raise ItemSchemaError("canonical ContextRef 必须使用 item content_hash")
            if self.item_sequence is not None and (
                not isinstance(self.item_sequence, int)
                or isinstance(self.item_sequence, bool)
                or self.item_sequence <= 0
            ):
                raise ItemSchemaError("canonical ContextRef.item_sequence 必须是正整数")
            if self.source_ref is not None:
                raise ItemSchemaError("canonical ContextRef 不得携带 detail/source ref")
            if (
                self.base_delta_role != BaseDeltaRole.NONE
                or self.source_overlay_epoch is not None
            ):
                raise ItemSchemaError(
                    "canonical ContextRef 不得携带 source overlay binding"
                )
            if any(
                value is not None
                for value in (
                    self.overlay_from_revision,
                    self.overlay_to_revision,
                    self.overlay_diff_hash,
                )
            ):
                raise ItemSchemaError(
                    "canonical ContextRef 不得携带 overlay diff binding"
                )
        else:
            if self.source_ref is not None:
                if isinstance(self.source_ref, DetailRef):
                    self.source_ref.require_owner(self.session_id)
                else:
                    _non_empty_string(self.source_ref, "ContextRef.source_ref")
            if self.availability == "available":
                _non_empty_string(self.source_revision, "ContextRef.source_revision")
                if self.content_length is None:
                    raise ItemSchemaError(
                        "available request_only ContextRef 必须带 content_length"
                    )
            elif self.source_revision is not None:
                _non_empty_string(self.source_revision, "ContextRef.source_revision")
            if self.availability == "available" and self.source_ref is None:
                raise ItemSchemaError(
                    "request_only ContextRef 必须带 source_ref"
                )
            if self.semantic_kind is not None:
                _non_empty_string(self.semantic_kind, "ContextRef.semantic_kind")
            if self.payload_kind is not None:
                _non_empty_string(self.payload_kind, "ContextRef.payload_kind")
            if (
                self.base_delta_role
                in {
                    BaseDeltaRole.BASE,
                    BaseDeltaRole.DELTA,
                }
                and self.source_overlay_epoch is None
                and self.availability == "available"
            ):
                raise ItemSchemaError("overlay ContextRef 必须带 source_overlay_epoch")
            for name in (
                "overlay_from_revision",
                "overlay_to_revision",
                "overlay_diff_hash",
            ):
                value = getattr(self, name)
                if value is not None:
                    _non_empty_string(value, f"ContextRef.{name}")
            if self.availability == "available" and self.base_delta_role == BaseDeltaRole.DELTA and not all(
                value is not None
                for value in (
                    self.overlay_from_revision,
                    self.overlay_to_revision,
                    self.overlay_diff_hash,
                )
            ):
                raise ItemSchemaError(
                    "delta ContextRef 必须带 from/to revision 与 diff_hash"
                )

    @classmethod
    def canonical_item(cls, item: CanonicalItemRecord, *, session_id: str) -> ContextRef:
        if not isinstance(item, CanonicalItemRecord):
            raise TypeError("canonical ContextRef 只接受 CanonicalItemRecord")
        if "source_revision" in item.metadata:
            source_revision = _non_empty_string(
                item.metadata["source_revision"],
                "CanonicalItemRecord.metadata.source_revision",
            )
        else:
            source_revision = f"canonical:{item.item_id}:{item.content_hash}"
        visibility_value = item.metadata.get("visibility", "internal")
        protection_value = item.metadata.get("protection", "public")
        return cls(
            ref_type="canonical_item",
            ref_id=item.item_id,
            session_id=session_id,
            semantic_kind=item.semantic_kind,
            payload_kind=item.payload_kind,
            status=item.status,
            item_sequence=item.item_sequence,
            source_revision=source_revision,
            content_length=payload_content_length(item.payload_kind, item.payload),
            content_hash=item.content_hash,
            visibility=_non_empty_string(
                visibility_value,
                "CanonicalItemRecord.metadata.visibility",
            ),
            protection=_non_empty_string(
                protection_value,
                "CanonicalItemRecord.metadata.protection",
            ),
        )

    @classmethod
    def request_only_ref(
        cls,
        ref_id: str,
        *,
        session_id: str,
        plan_id: str,
        source_revision: str,
        semantic_kind: str | None = None,
        payload_kind: str | None = None,
        content: object | None = None,
        content_length: int | None = None,
        content_hash_value: str | None = None,
        redacted_stable_digest: str | None = None,
        source_ref: str | DetailRef | None = None,
        base_delta_role: str = "none",
        source_overlay_epoch: int | None = None,
        overlay_from_revision: str | None = None,
        overlay_to_revision: str | None = None,
        overlay_diff_hash: str | None = None,
        protection: str = "public",
        visibility: str = "internal",
        availability: str = "available",
    ) -> ContextRef:
        _non_empty_string(ref_id, "ContextRef.ref_id")
        _non_empty_string(source_revision, "ContextRef.source_revision")
        if payload_kind is not None:
            _non_empty_string(payload_kind, "ContextRef.payload_kind")
            if payload_kind not in {item.value for item in PayloadKind}:
                raise ItemSchemaError(f"未知 ContextRef.payload_kind: {payload_kind}")
        effective_payload_kind = (
            PayloadKind.STRUCTURED_CONTENT if payload_kind is None else payload_kind
        )
        actual_length = (
            payload_content_length(effective_payload_kind, content)
            if content is not None
            else None
        )
        if content is not None:
            if content_length is not None and content_length != actual_length:
                raise ItemSchemaError(
                    f"request-only ref content_length 与正文不一致: {ref_id}"
                )
            if redacted_stable_digest is not None:
                raise ItemSchemaError(
                    f"带正文的 request-only ref 不得使用 redacted digest: {ref_id}"
                )
            actual_hash = sha256_jcs(content)
            if content_hash_value is not None and content_hash_value != actual_hash:
                raise ItemSchemaError(
                    f"request-only ref content_hash 与正文不一致: {ref_id}"
                )
            body_length = actual_length
            body_hash = actual_hash
        else:
            body_length = content_length
            body_hash = content_hash_value
        return cls(
            ref_type="request_only",
            ref_id=ref_id,
            session_id=session_id,
            plan_id=plan_id,
            source_revision=source_revision,
            semantic_kind=semantic_kind,
            payload_kind=payload_kind,
            content_length=body_length,
            content_hash=body_hash,
            redacted_stable_digest=redacted_stable_digest,
            source_ref=source_ref if source_ref is not None else ref_id,
            base_delta_role=base_delta_role,
            source_overlay_epoch=source_overlay_epoch,
            overlay_from_revision=overlay_from_revision,
            overlay_to_revision=overlay_to_revision,
            overlay_diff_hash=overlay_diff_hash,
            protection=protection,
            visibility=visibility,
            availability=availability,
        )

    def to_dict(self) -> dict[str, object]:
        # ContextRef 是 sealed assembly 的 tagged manifest。即使 optional
        # omission 没有正文，也必须保留可空字段的显式 null；否则同一份
        # snapshot 在恢复时无法区分“字段缺失”与“已知但不可用”。
        result: dict[str, object] = {
            "ref_type": self.ref_type,
            "ref_id": self.ref_id,
            "session_id": self.session_id,
            "plan_id": self.plan_id,
            "semantic_kind": self.semantic_kind,
            "payload_kind": self.payload_kind,
            "status": self.status,
            "item_sequence": self.item_sequence,
            "source_revision": self.source_revision,
            "content_length": self.content_length,
            "source_ref": (
                self.source_ref.to_dict() if isinstance(self.source_ref, DetailRef)
                else self.source_ref
            ),
            "base_delta_role": self.base_delta_role,
            "source_overlay_epoch": self.source_overlay_epoch,
            "overlay_from_revision": self.overlay_from_revision,
            "overlay_to_revision": self.overlay_to_revision,
            "overlay_diff_hash": self.overlay_diff_hash,
            "visibility": self.visibility,
            "protection": self.protection,
            "availability": self.availability,
            "content_hash": self.content_hash,
            "redacted_stable_digest": self.redacted_stable_digest,
        }
        return result


@dataclass(frozen=True, slots=True)
class ToolSetRef:
    """Provider tool manifest 的独立 selection source。"""

    ref_id: str
    session_id: str
    plan_id: str
    source_revision: str
    tool_set_schema: str
    tool_set_schema_version: str
    tool_policy_version: str
    content_length: int
    content_hash: str | None = None
    redacted_stable_digest: str | None = None
    protection: str = DetailProtection.PUBLIC
    availability: str = DetailAvailability.AVAILABLE
    tool_policy: Mapping[str, object] = field(default_factory=dict)
    tools: tuple[Mapping[str, object], ...] = ()
    assembly_id: str | None = None
    ref_type: str = "tool_set"

    @classmethod
    def from_tool_snapshot(
        cls,
        *,
        snapshot_id: str,
        session_id: str,
        plan_id: str,
        tools: Sequence[Mapping[str, object]],
        source_revision: str,
        tool_policy: Mapping[str, object] | None = None,
        tool_set_schema: str = "openai.tools",
        tool_set_schema_version: str = "v1",
        tool_policy_version: str = "v1",
        assembly_id: str | None = None,
    ) -> ToolSetRef:
        """从 provider-neutral tool snapshot 建立带 manifest 的 ToolSetRef。"""
        for value, field_name in (
            (snapshot_id, "ToolSetRef.snapshot_id"),
            (session_id, "ToolSetRef.session_id"),
            (plan_id, "ToolSetRef.plan_id"),
            (source_revision, "ToolSetRef.source_revision"),
            (tool_set_schema, "ToolSetRef.tool_set_schema"),
            (tool_set_schema_version, "ToolSetRef.tool_set_schema_version"),
            (tool_policy_version, "ToolSetRef.tool_policy_version"),
        ):
            _non_empty_string(value, field_name)
        if isinstance(tools, (str, bytes)):
            raise TypeError("ToolSetRef.tools 必须是 mapping 序列")
        if tool_policy is not None and not isinstance(tool_policy, Mapping):
            raise TypeError("ToolSetRef.tool_policy 必须是 object")

        normalized_tools: list[dict[str, object]] = []
        for tool in tools:
            if not isinstance(tool, Mapping):
                raise TypeError("ToolSetRef.tools 元素必须是 object")
            normalized = dict(tool)
            tool_id = _tool_identity(normalized)
            _non_empty_string(tool_id, "ToolSetRef.tools[].tool_id")
            normalized_tools.append(normalized)
        ordered_tools = tuple(sorted(normalized_tools, key=_tool_identity))
        policy = dict(tool_policy) if tool_policy is not None else {}
        manifest = {
            "tool_set_schema": tool_set_schema,
            "tool_set_schema_version": tool_set_schema_version,
            "tools": [dict(tool) for tool in ordered_tools],
            "tool_policy": policy,
            "tool_policy_version": tool_policy_version,
        }
        encoded = canonical_json_bytes(manifest)
        return cls(
            ref_id=snapshot_id,
            session_id=session_id,
            plan_id=plan_id,
            source_revision=source_revision,
            tool_set_schema=tool_set_schema,
            tool_set_schema_version=tool_set_schema_version,
            tool_policy_version=tool_policy_version,
            content_length=len(encoded),
            content_hash="sha256:jcs:v1:" + hashlib.sha256(encoded).hexdigest(),
            protection=DetailProtection.PUBLIC,
            availability=DetailAvailability.AVAILABLE,
            tool_policy=policy,
            tools=ordered_tools,
            assembly_id=assembly_id,
        )

    def __post_init__(self) -> None:
        for name in (
            "ref_id",
            "session_id",
            "plan_id",
            "source_revision",
            "tool_set_schema",
            "tool_set_schema_version",
            "tool_policy_version",
        ):
            _non_empty_string(getattr(self, name), f"ToolSetRef.{name}")
        if self.ref_type != "tool_set":
            raise ItemSchemaError("ToolSetRef.ref_type 必须是 tool_set")
        if (
            not isinstance(self.content_length, int)
            or isinstance(self.content_length, bool)
            or self.content_length < 0
        ):
            raise ItemSchemaError("ToolSetRef.content_length 必须是非负整数")
        if (self.content_hash is None) == (self.redacted_stable_digest is None):
            raise ItemSchemaError(
                "ToolSetRef 必须恰好包含 content_hash 或 redacted_stable_digest"
            )
        if self.content_hash is not None:
            validate_hash_token(self.content_hash, "ToolSetRef.content_hash")
        if self.redacted_stable_digest is not None:
            validate_hash_token(
                self.redacted_stable_digest,
                "ToolSetRef.redacted_stable_digest",
                redacted=True,
            )
        if (
            self.redacted_stable_digest is not None
            and self.protection == DetailProtection.PUBLIC
        ):
            raise ItemSchemaError(
                "带 redacted_stable_digest 的 ToolSetRef 必须声明 redacted 或 protected protection"
            )
        if not isinstance(self.protection, str) or self.protection not in {
            item.value for item in DetailProtection
        }:
            raise ItemSchemaError(f"未知 ToolSetRef.protection: {self.protection}")
        if not isinstance(self.availability, str) or self.availability not in {
            item.value for item in DetailAvailability
        }:
            raise ItemSchemaError(f"未知 ToolSetRef.availability: {self.availability}")
        if self.assembly_id is not None:
            _non_empty_string(self.assembly_id, "ToolSetRef.assembly_id")
        if not isinstance(self.tool_policy, Mapping):
            raise ItemSchemaError("ToolSetRef.tool_policy 必须是 object")
        if not isinstance(self.tools, tuple):
            raise ItemSchemaError("ToolSetRef.tools 必须是 tuple")
        if self.protection != DetailProtection.PUBLIC and (
            self.tools or self.tool_policy
        ):
            raise ItemSchemaError(
                "受保护或脱敏 ToolSetRef 不得携带原始 tools/tool_policy"
            )
        tool_ids: list[str] = []
        for tool in self.tools:
            if not isinstance(tool, Mapping):
                raise ItemSchemaError("ToolSetRef.tools 元素必须是 object")
            tool_ids.append(
                _non_empty_string(_tool_identity(tool), "ToolSetRef.tools[].tool_id")
            )
        if tool_ids != sorted(tool_ids):
            raise ItemSchemaError("ToolSetRef.tools 必须按 tool_id 稳定排序")
        if len(tool_ids) != len(set(tool_ids)):
            raise ItemSchemaError("ToolSetRef.tools 不得包含重复 tool_id")

    def manifest_preimage(self) -> dict[str, object]:
        return {
            "tool_set_schema": self.tool_set_schema,
            "tool_set_schema_version": self.tool_set_schema_version,
            "tools": [dict(tool) for tool in self.tools],
            "tool_policy": dict(self.tool_policy),
            "tool_policy_version": self.tool_policy_version,
        }

    def validate_manifest(self) -> None:
        # 脱敏/受保护的 registry entry 只携带稳定摘要和原文长度；普通
        # reader 不能拿空的 tools/tool_policy 重建伪 manifest，也不能把
        # metadata entry 误判为一个长度/哈希不匹配的公开 manifest。真正
        # 的 provider dispatch 仍会在 project_tool_set_ref 中因缺少
        # content_hash 而返回 detail-unavailable。
        if self.redacted_stable_digest is not None:
            if self.protection == DetailProtection.PUBLIC:
                raise ItemSchemaError("redacted ToolSetRef 不得声明 public protection")
            return
        encoded = canonical_json_bytes(self.manifest_preimage())
        if len(encoded) != self.content_length:
            raise ItemSchemaError("ToolSetRef manifest content_length 不匹配")
        digest = "sha256:jcs:v1:" + hashlib.sha256(encoded).hexdigest()
        if self.content_hash is not None and self.content_hash != digest:
            raise ItemSchemaError("ToolSetRef manifest content_hash 不匹配")

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "ref_type": self.ref_type,
            "ref_id": self.ref_id,
            "plan_id": self.plan_id,
            "session_id": self.session_id,
            "assembly_id": self.assembly_id,
            "source_revision": self.source_revision,
            "tool_set_schema": self.tool_set_schema,
            "tool_set_schema_version": self.tool_set_schema_version,
            "tool_policy_version": self.tool_policy_version,
            "content_length": self.content_length,
            "protection": self.protection,
            "availability": self.availability,
            "tool_policy": dict(self.tool_policy),
            "tools": [dict(tool) for tool in self.tools],
        }
        # manifest token 的 key 固定存在；另一侧使用 null 表示未采用。
        result["content_hash"] = self.content_hash
        result["redacted_stable_digest"] = self.redacted_stable_digest
        return result


__all__ = [
    "ContextRef",
    "ToolSetRef",
    "ref_identity",
    "require_manifest_token",
    "unique_ref_identities",
]
