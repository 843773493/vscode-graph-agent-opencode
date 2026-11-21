"""从封存 activation 恢复工具定义与分派身份，不读取实时目录。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from app.agents.model_tool_schema import model_tool_schema_validator
from app.agents.policy.tool_capability import ToolMetadata
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.resource_activation import (
    ResourceActivationSnapshotRef,
    ResourceProvenanceRef,
)
from app.services.infrastructure.mcp.extension_catalog import (
    EXTENSION_CATALOG_SOURCE_ID,
    MCP_TOOL_CATALOG_RESOURCE_ID,
    ExtensionCatalogUnavailableError,
    ExtensionDispatchBindingRef,
    extension_catalog_binding_from_members,
    extension_schema_hash,
    member_readiness,
)
from app.services.infrastructure.mcp.tool_definitions import (
    DEFINITION_FACET,
    SkillToolAssociation,
    ToolDefinition,
    definition_resource_id,
)


def mcp_guidance_content(payload: object) -> str:
    """资源正文严格只包含选中的 guidance，不容纳成员或 provenance。"""
    if not isinstance(payload, Mapping) or set(payload) != {"content"} or not isinstance(payload["content"], str):
        raise ExtensionCatalogUnavailableError("mcp_tool_catalog protected payload 不是完整的 guidance 正文")
    return payload["content"]


def _frozen_skill_tool_associations(payload: object) -> tuple[SkillToolAssociation, ...]:
    content = mcp_guidance_content(payload)
    try:
        guidance = json.loads(content)
    except json.JSONDecodeError as error:
        raise ExtensionCatalogUnavailableError("冻结工具 guidance 不是有效 JSON") from error
    if not isinstance(guidance, Mapping):
        raise ExtensionCatalogUnavailableError("冻结工具 guidance 必须是对象")
    raw = guidance.get("skill_associations", [])
    if not isinstance(raw, list):
        raise ExtensionCatalogUnavailableError("冻结工具 guidance Skill 关联必须为数组")
    values = tuple(SkillToolAssociation.from_dict(item) for item in raw)
    if len({(item.tool_id, item.skill_name, item.entry_identity) for item in values}) != len(values):
        raise ExtensionCatalogUnavailableError("冻结目录包含重复 Skill 工具来源关联")
    return values


def restore_frozen_extension_dispatch(
    *, snapshot: ResourceActivationSnapshotRef, binding: ResourceProvenanceRef,
    payload: object, model_call_id: str,
) -> ExtensionDispatchBindingRef:
    """只由 Saver 恢复的冻结父行/typed members/lineage 重建独立 dispatch 值。"""
    lineage_id, revision = _frozen_extension_catalog_revision(snapshot=snapshot, binding=binding, payload=payload)
    if lineage_id != EXTENSION_CATALOG_SOURCE_ID:
        raise ExtensionCatalogUnavailableError(
            "sealed extension catalog 缺少精确执行身份 v3 来源；历史可读但不可恢复执行，需新模型调用"
        )
    catalog = extension_catalog_binding_from_members(
        catalog_revision=revision, generation=binding.captured_registry_generation,
        members=binding.members,
    )
    return ExtensionDispatchBindingRef(
        binding_ref=catalog, guidance_revision=binding.revision,
        activation_policy_revision=snapshot.activation_policy_revision,
        owner_session_id=snapshot.owner_session_id, owner_thread_id=snapshot.owner_thread_id,
        execution_id=snapshot.execution_id, turn_id=snapshot.turn_id,
        model_call_id=model_call_id,
        catalog_snapshot_ref=snapshot.activation_snapshot_id,
    )


def _frozen_extension_catalog_revision(
    *, snapshot: ResourceActivationSnapshotRef, binding: ResourceProvenanceRef, payload: object,
) -> tuple[str, str]:
    """完整定义的只读历史合同不要求旧 socket 仍然可执行。"""
    mcp_guidance_content(payload)
    if binding.resource_id != MCP_TOOL_CATALOG_RESOURCE_ID or binding.resource_kind != "mcp_tool_catalog" or binding not in snapshot.bindings:
        raise ExtensionCatalogUnavailableError("sealed activation 缺少唯一 mcp_tool_catalog 父行")
    if binding.source_lineage_ref is None:
        raise ExtensionCatalogUnavailableError("sealed extension catalog lineage 缺失")
    # TODO: 历史 v2 完整 schema 继续沿唯一冻结读取链展示；只允许 v3 恢复执行。
    revisions = tuple(
        (identity, revision) for identity, revision in binding.source_lineage_ref.sources
        if identity in {"extension-catalog:v2", EXTENSION_CATALOG_SOURCE_ID}
    )
    if len(revisions) != 1:
        raise ExtensionCatalogUnavailableError(
            "sealed extension catalog 缺少唯一完整 schema 来源"
        )
    return revisions[0]


@dataclass(frozen=True, slots=True)
class FrozenToolDefinition(ToolDefinition):
    server_id: str | None
    provenance: ResourceProvenanceRef
    _resource_includes_skill_associations: bool = field(default=True, kw_only=True, repr=False)

    @property
    def resource_payload(self) -> dict[str, object]:
        payload = super(FrozenToolDefinition, self).resource_payload
        # 保留读取器既有的四字段冻结正文；补上空关联也会改变其原 revision。
        if not self._resource_includes_skill_associations:
            del payload["skill_associations"]
        return payload


@dataclass(frozen=True, slots=True)
class FrozenToolCatalog:
    """冻结目录的定义视图；按 ID 选取时仍保留完整目录的 revision。"""

    catalog_revision: str
    activation_snapshot_id: str
    definitions: tuple[FrozenToolDefinition, ...]
    skill_associations: tuple[SkillToolAssociation, ...] = ()


def restore_frozen_tool_catalog(
    snapshot: ResourceActivationSnapshotRef,
    read_payload: Callable[[ResourceActivationSnapshotRef, ResourceProvenanceRef], object],
    *, tool_ids: frozenset[str] | None = None,
) -> FrozenToolCatalog:
    """只解析调用所属快照；缺失历史定义时拒绝，不从 live 同名工具补齐。"""
    parents = tuple(binding for binding in snapshot.bindings if binding.resource_id == MCP_TOOL_CATALOG_RESOURCE_ID)
    if len(parents) != 1:
        raise ExtensionCatalogUnavailableError("冻结扩展目录父行缺失或不唯一")
    parent = parents[0]
    parent_payload = read_payload(snapshot, parent)
    lineage_id, revision = _frozen_extension_catalog_revision(
        snapshot=snapshot, binding=parent, payload=parent_payload,
    )
    skill_associations = _frozen_skill_tool_associations(parent_payload)
    facets = {binding.resource_id: binding for binding in snapshot.bindings
              if binding.resource_kind == "mcp_tool_catalog" and binding.facet == DEFINITION_FACET}
    definitions = []
    for member in parent.members:
        # 准备只消费已发现的定义；完整搜索仍读取全部正文，不跨操作保留校验结果。
        if tool_ids is not None and member.member_id not in tool_ids:
            continue
        binding = facets.get(definition_resource_id(member.member_id))
        # lineage_id 由 resource_id 派生，来源事实必须逐项相等。
        if (binding is None or binding.members != (member,)
                or binding.source_lineage_ref is None or parent.source_lineage_ref is None
                or binding.source_lineage_ref.sources != parent.source_lineage_ref.sources
                or binding.captured_registry_generation != parent.captured_registry_generation):
            raise ExtensionCatalogUnavailableError(f"冻结完整定义与目录身份不一致: {member.member_id}")
        payload = read_payload(snapshot, binding)
        if (not isinstance(payload, Mapping)
                or set(payload) not in ({"description", "input_schema", "kind", "group_id"},
                                        {"description", "input_schema", "kind", "group_id", "skill_associations"})
                or any(not isinstance(payload[key], str) for key in ("description", "kind", "group_id"))
                or not isinstance(payload["input_schema"], dict) or sha256_jcs(payload) != binding.revision
                or extension_schema_hash(payload["input_schema"]) != member.schema_hash):
            raise ExtensionCatalogUnavailableError(f"冻结完整定义正文或 schema 身份无效: {member.member_id}")
        associations = tuple(
            SkillToolAssociation.from_dict(item) for item in payload.get("skill_associations", [])
        )
        if any(item.tool_id != member.member_id for item in associations):
            raise ExtensionCatalogUnavailableError(f"冻结工具定义含其他 tool_id 的 Skill 关联: {member.member_id}")
        model_tool_schema_validator(payload["input_schema"])
        definitions.append(FrozenToolDefinition(
            metadata=ToolMetadata(member.member_id, member.member_kind, payload["kind"], payload["group_id"]),
            schema_hash=member.schema_hash, server_id=member.server_id, provenance=binding,
            description=payload["description"], _input_schema_json=canonical_json_bytes(payload["input_schema"]),
            skill_associations=associations,
            _resource_includes_skill_associations="skill_associations" in payload,
            # TODO: 历史 v2 没有连接身份，只沿原合同恢复定义；v3 执行入口仍拒绝其 lineage。
            readiness=member_readiness(member) if lineage_id == EXTENSION_CATALOG_SOURCE_ID else "ready",
        ))
    associations_by_source = {
        (item.tool_id, item.skill_name, item.entry_identity): item for item in skill_associations
    }
    for definition in definitions:
        for association in definition.skill_associations:
            key = (association.tool_id, association.skill_name, association.entry_identity)
            existing = associations_by_source.get(key)
            if existing is None or (existing.status == "unavailable" and association.status == "available"):
                associations_by_source[key] = association
    all_skill_associations = tuple(sorted(
        associations_by_source.values(),
        key=lambda item: (item.skill_name, item.tool_id, item.entry_identity or ""),
    ))
    return FrozenToolCatalog(
        revision, snapshot.activation_snapshot_id, tuple(definitions), all_skill_associations,
    )
