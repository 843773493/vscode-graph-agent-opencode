"""同一扩展目录的完整定义 facet；正文仅由既有资源存储冻结和恢复。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

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
from app.services.infrastructure.mcp.extension_catalog import (
    ConnectionReadiness,
    ExtensionCatalogUnavailableError,
    extension_schema_hash,
    member_readiness,
)

DEFINITION_FACET = "definition"


def definition_resource_id(tool_id: str) -> str:
    return "extension-tool-definition:v1:" + sha256_jcs(tool_id).rsplit(":", 1)[1]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    metadata: ToolMetadata
    schema_hash: str
    description: str
    _input_schema_json: bytes
    readiness: ConnectionReadiness = field(default="ready", kw_only=True)

    @property
    def payload(self) -> dict[str, object]:
        return {
            "description": self.description, "input_schema": self.input_schema,
            "kind": self.metadata.kind, "group_id": self.metadata.group_id,
        }

    @property
    def input_schema(self) -> dict[str, object]:
        # 描述检索不解码 schema；完整契约每次返回独立值，不共享可变 JSON。
        return json.loads(self._input_schema_json)


def definition_from_tool(tool: BaseTool, metadata: ToolMetadata) -> ToolDefinition:
    """只导出一次完整参数契约，摘要与冻结正文共用该值。"""
    schema = export_model_tool_json_schema(tool)
    return ToolDefinition(metadata, extension_schema_hash(schema), tool.description, canonical_json_bytes(schema))


@dataclass(frozen=True, slots=True)
class FrozenToolDefinition(ToolDefinition):
    server_id: str | None
    provenance: ResourceProvenanceRef


@dataclass(frozen=True, slots=True)
class FrozenToolCatalog:
    catalog_revision: str
    activation_snapshot_id: str
    definitions: tuple[FrozenToolDefinition, ...]


def tool_catalog_from_activation(
    snapshot: ResourceActivationSnapshotRef,
    read_payload: Callable[[ResourceActivationSnapshotRef, ResourceProvenanceRef], object],
) -> FrozenToolCatalog:
    """只解析调用所属快照；缺失历史定义时拒绝，不从 live 同名工具补齐。"""
    # 发布模块依赖本模块，读取路径延迟引用以免形成模块初始化环。
    from app.services.infrastructure.mcp.catalog_activation import (
        EXTENSION_CATALOG_SOURCE_ID,
        MCP_TOOL_CATALOG_RESOURCE_ID,
        frozen_extension_catalog_revision,
    )

    parents = tuple(binding for binding in snapshot.bindings if binding.resource_id == MCP_TOOL_CATALOG_RESOURCE_ID)
    if len(parents) != 1:
        raise ExtensionCatalogUnavailableError("冻结扩展目录父行缺失或不唯一")
    parent = parents[0]
    lineage_id, revision = frozen_extension_catalog_revision(
        snapshot=snapshot, binding=parent, payload=read_payload(snapshot, parent),
    )
    facets = {binding.resource_id: binding for binding in snapshot.bindings
              if binding.resource_kind == "mcp_tool_catalog" and binding.facet == DEFINITION_FACET}
    definitions = []
    for member in parent.members:
        binding = facets.get(definition_resource_id(member.member_id))
        # lineage_id 由 resource_id 派生，来源事实必须逐项相等。
        if (binding is None or binding.members != (member,)
                or binding.source_lineage_ref is None or parent.source_lineage_ref is None
                or binding.source_lineage_ref.sources != parent.source_lineage_ref.sources
                or binding.captured_registry_generation != parent.captured_registry_generation):
            raise ExtensionCatalogUnavailableError(f"冻结完整定义与目录身份不一致: {member.member_id}")
        payload = read_payload(snapshot, binding)
        if (not isinstance(payload, Mapping) or set(payload) != {"description", "input_schema", "kind", "group_id"}
                or any(not isinstance(payload[key], str) for key in ("description", "kind", "group_id"))
                or not isinstance(payload["input_schema"], dict) or sha256_jcs(payload) != binding.revision
                or extension_schema_hash(payload["input_schema"]) != member.schema_hash):
            raise ExtensionCatalogUnavailableError(f"冻结完整定义正文或 schema 身份无效: {member.member_id}")
        model_tool_schema_validator(payload["input_schema"])
        definitions.append(FrozenToolDefinition(
            metadata=ToolMetadata(member.member_id, member.member_kind, payload["kind"], payload["group_id"]),
            schema_hash=member.schema_hash, server_id=member.server_id, provenance=binding,
            description=payload["description"], _input_schema_json=canonical_json_bytes(payload["input_schema"]),
            # TODO: 历史 v2 没有连接身份，只沿原合同恢复定义；v3 执行入口仍拒绝其 lineage。
            readiness=member_readiness(member) if lineage_id == EXTENSION_CATALOG_SOURCE_ID else "ready",
        ))
    return FrozenToolCatalog(revision, snapshot.activation_snapshot_id, tuple(definitions))
