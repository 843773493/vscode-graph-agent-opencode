"""实时扩展工具的完整定义、来源身份与 Skill 关联。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from langchain_core.tools import BaseTool

from app.agents.model_tool_schema import (
    export_model_tool_json_schema,
)
from app.agents.policy.tool_capability import ToolMetadata
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.schemas.gateway.skill_sources import PluginSkillSourceDTO
from app.services.infrastructure.mcp.extension_catalog import (
    ConnectionReadiness,
    extension_schema_hash,
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
    """工具契约与发布资源：后者还包含参与资源 revision 的 Skill 关联。"""

    metadata: ToolMetadata
    schema_hash: str
    description: str
    _input_schema_json: bytes
    readiness: ConnectionReadiness = field(default="ready", kw_only=True)
    skill_associations: tuple[SkillToolAssociation, ...] = field(default=(), kw_only=True)
    plugin_tool_source: PluginToolSource | None = field(default=None, kw_only=True, repr=False)
    product_tool_contract: tuple[str, int] | None = field(default=None, kw_only=True, repr=False)

    @property
    def contract_payload(self) -> dict[str, object]:
        return {
            "description": self.description, "input_schema": self.input_schema,
            "kind": self.metadata.kind, "group_id": self.metadata.group_id,
        }

    @property
    def resource_payload(self) -> dict[str, object]:
        return {**self.contract_payload, "skill_associations": [item.to_dict() for item in self.skill_associations]}

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


def plugin_tool_source_from_tool(tool: BaseTool) -> PluginToolSource | None:
    value = (tool.metadata or {}).get("plugin_tool_source")
    return None if value is None else PluginToolSource.from_metadata(value)
