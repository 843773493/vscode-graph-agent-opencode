"""同一扩展目录的完整定义 facet；正文仅由既有资源存储冻结和恢复。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from langchain_core.tools import BaseTool

from app.agents.model_tool_schema import (
    export_model_tool_json_schema,
    model_tool_schema_validator,
)
from app.agents.policy.tool_capability import ToolMetadata
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.resource_activation import (
    ResourceActivationSnapshotRef,
    ResourceProvenanceRef,
)
from app.schemas.gateway.skill_sources import PluginSkillSourceDTO
from app.services.infrastructure.mcp.extension_catalog import (
    ConnectionReadiness,
    ExtensionCatalogUnavailableError,
    extension_schema_hash,
    member_readiness,
)

DEFINITION_FACET = "definition"


@dataclass(frozen=True, slots=True)
class PluginToolSource:
    """工具 producer 对实际插件服务注册的窄身份声明。"""

    gateway_id: str
    package_id: str
    plugin_id: str
    package_version: str
    package_digest: str
    service_id: str
    workspace_id: str | None
    contract_version: int
    instance_id: str | None = None

    @classmethod
    def from_metadata(cls, value: object) -> PluginToolSource:
        if not isinstance(value, Mapping) or set(value) != {
            "gateway_id", "package_id", "plugin_id", "package_version", "package_digest",
            "service_id", "workspace_id", "contract_version", "instance_id",
        }:
            raise ValueError("插件工具 source metadata 字段不完整或含未登记字段")
        string_fields = ("gateway_id", "package_id", "plugin_id", "package_version", "package_digest", "service_id")
        if any(not isinstance(value[field], str) or not value[field] for field in string_fields):
            raise ValueError("插件工具 source metadata 身份字段必须是非空字符串")
        if not re.fullmatch(r"[a-f0-9]{64}", value["package_digest"]):
            raise ValueError("插件工具 source metadata package_digest 无效")
        if value["workspace_id"] is not None and not isinstance(value["workspace_id"], str):
            raise ValueError("插件工具 source metadata workspace_id 无效")
        if type(value["contract_version"]) is not int or value["contract_version"] <= 0:
            raise ValueError("插件工具 source metadata contract_version 无效")
        if value["instance_id"] is not None and not isinstance(value["instance_id"], str):
            raise ValueError("插件工具 source metadata instance_id 无效")
        return cls(**value)

    def to_dict(self) -> dict[str, object]:
        return {
            "gateway_id": self.gateway_id, "package_id": self.package_id,
            "plugin_id": self.plugin_id, "package_version": self.package_version,
            "package_digest": self.package_digest, "service_id": self.service_id,
            "workspace_id": self.workspace_id, "contract_version": self.contract_version,
            "instance_id": self.instance_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> PluginToolSource:
        return cls.from_metadata(value)


@dataclass(frozen=True, slots=True)
class SkillToolAssociation:
    """本次已发布目录中，工具与确切 Skill 来源的关联结果。"""

    skill_name: str
    tool_id: str
    source_id: str | None
    entry_identity: str | None
    display_uri: str | None
    status: Literal["available", "unavailable"]
    reason: str | None = None
    tool_source: PluginToolSource | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "skill_name": self.skill_name, "tool_id": self.tool_id, "source_id": self.source_id,
            "entry_identity": self.entry_identity, "display_uri": self.display_uri,
            "status": self.status,
        }
        if self.reason is not None:
            result["reason"] = self.reason
        if self.tool_source is not None:
            result["tool_source"] = self.tool_source.to_dict()
        return result

    @classmethod
    def from_dict(cls, value: object) -> SkillToolAssociation:
        required = {"skill_name", "tool_id", "source_id", "entry_identity", "display_uri", "status"}
        optional = {"reason", "tool_source"}
        if not isinstance(value, Mapping) or not required.issubset(value) or set(value) - required - optional:
            raise ValueError("冻结 Skill 工具关联字段无效")
        if (not isinstance(value["skill_name"], str) or not value["skill_name"]
                or not isinstance(value["tool_id"], str) or not value["tool_id"]
                or value["status"] not in {"available", "unavailable"}):
            raise ValueError("冻结 Skill 工具关联身份或状态无效")
        for field_name in ("source_id", "entry_identity", "display_uri", "reason"):
            if value.get(field_name) is not None and not isinstance(value[field_name], str):
                raise ValueError(f"冻结 Skill 工具关联 {field_name} 无效")
        raw_source = value.get("tool_source")
        return cls(
            skill_name=value["skill_name"], tool_id=value["tool_id"],
            source_id=value["source_id"], entry_identity=value["entry_identity"],
            display_uri=value["display_uri"], status=value["status"], reason=value.get("reason"),
            tool_source=None if raw_source is None else PluginToolSource.from_dict(raw_source),
        )


@dataclass(frozen=True, slots=True)
class SkillToolSource:
    """已发布 Skill 来源的目录值；不包含或重新读取 Skill 正文。"""

    name: str
    source_id: str
    entry_identity: str
    display_uri: str
    plugin: PluginSkillSourceDTO | None = None
    effective: bool = True


def definition_resource_id(tool_id: str) -> str:
    return "extension-tool-definition:v1:" + sha256_jcs(tool_id).rsplit(":", 1)[1]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    metadata: ToolMetadata
    schema_hash: str
    description: str
    _input_schema_json: bytes
    readiness: ConnectionReadiness = field(default="ready", kw_only=True)
    skill_associations: tuple[SkillToolAssociation, ...] = field(default=(), kw_only=True)
    plugin_tool_source: PluginToolSource | None = field(default=None, kw_only=True, repr=False)
    product_tool_contract: tuple[str, int] | None = field(default=None, kw_only=True, repr=False)

    @property
    def payload(self) -> dict[str, object]:
        return {
            "description": self.description, "input_schema": self.input_schema,
            "kind": self.metadata.kind, "group_id": self.metadata.group_id,
        }

    def activation_payload(self) -> dict[str, object]:
        return {**self.payload, "skill_associations": [item.to_dict() for item in self.skill_associations]}

    @property
    def input_schema(self) -> dict[str, object]:
        # 描述检索不解码 schema；完整契约每次返回独立值，不共享可变 JSON。
        return json.loads(self._input_schema_json)


def definition_from_tool(tool: BaseTool, metadata: ToolMetadata) -> ToolDefinition:
    """只导出一次完整参数契约，摘要与冻结正文共用该值。"""
    schema = export_model_tool_json_schema(tool)
    return ToolDefinition(
        metadata, extension_schema_hash(schema), tool.description, canonical_json_bytes(schema),
        plugin_tool_source=plugin_tool_source_from_tool(tool),
        product_tool_contract=(
            (tool.name, 1) if metadata.origin == "custom"
            and (tool.metadata or {}).get("product_tool_contract") == (tool.name, 1) else None
        ),
    )


def _tool_skill_associations(
    tool_id: str,
    definition: ToolDefinition,
    skill_sources: Sequence[SkillToolSource],
    custom_skill_names: Sequence[str],
    *,
    gateway_id: str | None,
) -> tuple[SkillToolAssociation, ...]:
    effective_by_name = {source.name: source for source in skill_sources if source.effective}
    associations: dict[tuple[str | None, str], SkillToolAssociation] = {}

    for name in custom_skill_names:
        source = effective_by_name.get(name)
        association = SkillToolAssociation(
            skill_name=name, tool_id=tool_id,
            source_id=None if source is None else source.source_id,
            entry_identity=None if source is None else source.entry_identity,
            display_uri=None if source is None else source.display_uri,
            status="unavailable" if source is None else "available",
            reason="skill-not-in-published-catalog" if source is None else None,
        )
        associations[(association.entry_identity, association.skill_name)] = association

    for source in skill_sources:
        plugin = source.plugin
        if plugin is None:
            continue
        for binding in plugin.tool_bindings:
            if binding.tool_id != tool_id:
                continue
            reason = None
            tool_source = definition.plugin_tool_source
            if not source.effective:
                reason = "skill-source-not-effective"
            elif binding.service_id is None:
                if definition.product_tool_contract != (binding.tool_id, binding.contract_version):
                    reason = "product-tool-source-unverified"
                elif binding.schema_hash is not None and binding.schema_hash != definition.schema_hash:
                    reason = "tool-schema-hash-mismatch"
            elif tool_source is None:
                reason = "plugin-service-source-unverified"
            elif binding.contract_version != 1:
                reason = "tool-contract-version-unsupported"
            elif (
                gateway_id is None
                or tool_source.gateway_id != gateway_id
                or tool_source.package_id != plugin.package_id
                or tool_source.plugin_id != plugin.plugin_id
                or tool_source.package_version != plugin.package_version
                or tool_source.package_digest != plugin.package_digest
                or tool_source.workspace_id != plugin.workspace_id
                or tool_source.service_id != binding.service_id
                or tool_source.contract_version != binding.contract_version
            ):
                reason = "plugin-service-source-mismatch"
            elif definition.metadata.origin != "mcp":
                reason = "plugin-service-tool-origin-mismatch"
            elif definition.readiness != "ready":
                reason = "plugin-service-unavailable"
            elif binding.schema_hash is None:
                reason = "tool-schema-hash-missing"
            elif binding.schema_hash != definition.schema_hash:
                reason = "tool-schema-hash-mismatch"

            association = SkillToolAssociation(
                skill_name=source.name, tool_id=tool_id, source_id=source.source_id,
                entry_identity=source.entry_identity, display_uri=source.display_uri,
                status="unavailable" if reason is not None else "available",
                reason=reason, tool_source=tool_source,
            )
            key = (source.entry_identity, source.name)
            previous = associations.get(key)
            if previous is None or previous.status == "unavailable" and association.status == "available":
                associations[key] = association

    return tuple(sorted(associations.values(), key=lambda item: (item.skill_name, item.entry_identity or "")))



@dataclass(frozen=True, slots=True)
class FrozenToolDefinition(ToolDefinition):
    server_id: str | None
    provenance: ResourceProvenanceRef


def plugin_tool_source_from_tool(tool: BaseTool) -> PluginToolSource | None:
    value = (tool.metadata or {}).get("plugin_tool_source")
    return None if value is None else PluginToolSource.from_metadata(value)


@dataclass(frozen=True, slots=True)
class FrozenToolCatalog:
    """冻结目录的定义视图；按 ID 选取时仍保留完整目录的 revision。"""

    catalog_revision: str
    activation_snapshot_id: str
    definitions: tuple[FrozenToolDefinition, ...]
    skill_associations: tuple[SkillToolAssociation, ...] = ()


def tool_catalog_from_activation(
    snapshot: ResourceActivationSnapshotRef,
    read_payload: Callable[[ResourceActivationSnapshotRef, ResourceProvenanceRef], object],
    *, tool_ids: frozenset[str] | None = None,
) -> FrozenToolCatalog:
    """只解析调用所属快照；缺失历史定义时拒绝，不从 live 同名工具补齐。"""
    # 发布模块依赖本模块，读取路径延迟引用以免形成模块初始化环。
    from app.services.infrastructure.mcp.catalog_activation import (
        EXTENSION_CATALOG_SOURCE_ID,
        MCP_TOOL_CATALOG_RESOURCE_ID,
        frozen_extension_catalog_revision,
        frozen_skill_tool_associations,
    )

    parents = tuple(binding for binding in snapshot.bindings if binding.resource_id == MCP_TOOL_CATALOG_RESOURCE_ID)
    if len(parents) != 1:
        raise ExtensionCatalogUnavailableError("冻结扩展目录父行缺失或不唯一")
    parent = parents[0]
    parent_payload = read_payload(snapshot, parent)
    lineage_id, revision = frozen_extension_catalog_revision(
        snapshot=snapshot, binding=parent, payload=parent_payload,
    )
    skill_associations = frozen_skill_tool_associations(parent_payload)
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
