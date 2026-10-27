"""MCP Owner publication 到同一 runtime ResourceRegistry 的纯内存适配。

父行 payload 只承载实际选中的工具指引正文；完整定义使用同批 definition facet。
目录 revision、generation 与 typed members 进入独立 lineage/member 通道；
冻结与持久化归既有 Coordinator/Saver。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from langchain_core.tools import BaseTool

from app.agents.model_tool_schema import export_model_tool_json_schema
from app.agents.policy.tool_capability import (
    ToolCapabilityPolicy,
    ToolMetadata,
    metadata_for_tool,
)
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.resource_activation import (
    ResourceActivationSnapshotRef,
    ResourceProvenanceRef,
)
from app.services.infrastructure.mcp.catalog_owner import McpCatalogPublication
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionCatalogBindingError,
    ExtensionCatalogUnavailableError,
    ExtensionDispatchBindingRef,
    ExtensionTargetBindingInput,
    build_extension_catalog_binding,
    extension_catalog_binding_from_members,
    extension_schema_hash,
    payload_digest,
)
from app.services.infrastructure.mcp.tool_definitions import (
    DEFINITION_FACET,
    ToolDefinition,
    definition_payload,
    definition_resource_id,
)
from app.services.infrastructure.mcp.tool_guidance import extension_tool_guidance
from app.services.infrastructure.resource_platform.derivation.types import (
    ResourceSnapshot,
    SemanticResourceDescriptor,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)

MCP_TOOL_CATALOG_RESOURCE_ID = "mcp-tool-catalog:v1"
MCP_GUIDANCE_SOURCE_ID = "mcp-tool-guidance:v1"
MCP_OWNER_CATALOG_SOURCE_ID = "mcp-owner-catalog:v1"
CUSTOM_CATALOG_SOURCE_ID = "custom-extension-catalog:v1"
EXTENSION_CATALOG_SOURCE_ID = "extension-catalog:v3"


def mcp_guidance_content(payload: object) -> str:
    """资源正文严格只包含选中的 guidance，不容纳成员或 provenance。"""
    if not isinstance(payload, Mapping) or set(payload) != {"content"} or not isinstance(payload["content"], str):
        raise ExtensionCatalogUnavailableError("mcp_tool_catalog protected payload 不是完整的 guidance 正文")
    return payload["content"]


class McpCatalogResourcePublisher:
    """无 I/O、无独立 catalog，只把唯一 Owner 的完整值发布到调用方 Registry。"""

    def __init__(
        self, *, registry: ResourceRegistry, custom_tools: Sequence[BaseTool] = (),
        policy_resolver: Callable[[Sequence[ToolMetadata]], Mapping[str, ToolCapabilityPolicy]],
        custom_metadata: Mapping[str, ToolMetadata] | None = None,
    ) -> None:
        self._policy_resolver = policy_resolver
        self._registry = registry
        entries = tuple(sorted(custom_tools, key=lambda tool: tool.name))
        metadata = {tool.name: (custom_metadata or {}).get(tool.name) or metadata_for_tool(tool, origin="custom")
                    for tool in entries}
        self._custom_definitions = {tool.name: ToolDefinition(
            metadata[tool.name], extension_schema_hash(export_model_tool_json_schema(tool)),
            canonical_json_bytes(definition_payload(tool, metadata[tool.name])),
        ) for tool in entries}
        self._definition_ids: set[str] = set()
        self._custom_revision = payload_digest(
            [[name, definition.payload] for name, definition in self._custom_definitions.items()],
            context="工作区 custom extension typed members",
            error_type=ExtensionCatalogBindingError,
        )
        self._custom_members = build_extension_catalog_binding(
            catalog_revision=self._custom_revision, generation=1,
            targets=(ExtensionTargetBindingInput(
                target_id=tool.name, origin="custom", input_schema=export_model_tool_json_schema(tool),
            ) for tool in entries),
        ).members
        registry.register_descriptor(SemanticResourceDescriptor(
            resource_id=MCP_TOOL_CATALOG_RESOURCE_ID, resource_kind="mcp_tool_catalog",
            facet="activation", display_uri=None,
            source_ids=(MCP_OWNER_CATALOG_SOURCE_ID, CUSTOM_CATALOG_SOURCE_ID, EXTENSION_CATALOG_SOURCE_ID),
        ))

    def publish(self, publication: McpCatalogPublication) -> None:
        if not isinstance(publication, McpCatalogPublication):
            raise TypeError("MCP resource publisher 需要真实 Owner publication")
        definitions_by_id = {**publication.definitions, **self._custom_definitions}
        metadata = {name: definition.metadata for name, definition in definitions_by_id.items()}
        policies = self._policy_resolver(tuple(metadata.values()))
        if set(policies) != set(definitions_by_id):
            raise ValueError("扩展目录发布需要同一次选择快照的完整有效策略")
        # 冻结目录保存已知定义事实；策略只裁决展示与执行，不把禁用伪装成不存在。
        members = (*publication.binding_ref.members, *self._custom_members)
        revision = payload_digest(
            [publication.binding_ref.catalog_revision, self._custom_revision],
            context="唯一扩展目录 lineage",
            error_type=ExtensionCatalogBindingError,
        )
        lineage = (
            (MCP_OWNER_CATALOG_SOURCE_ID, publication.binding_ref.catalog_revision),
            (CUSTOM_CATALOG_SOURCE_ID, self._custom_revision),
            (EXTENSION_CATALOG_SOURCE_ID, revision),
        )
        if publication.status == "available":
            payload = {"content": extension_tool_guidance(tuple(definitions_by_id.values()), {
                name: policies[name] for name in definitions_by_id
            })}
            resource = ResourceSnapshot(
                resource_id=MCP_TOOL_CATALOG_RESOURCE_ID, resource_kind="mcp_tool_catalog",
                facet="activation", display_uri=None,
                revision=sha256_jcs(payload), payload=payload,
                source_lineage=lineage, generation=publication.binding_ref.generation,
                members=tuple(members),
            )
        else:
            retained = self._registry.latest_revision(MCP_TOOL_CATALOG_RESOURCE_ID)
            resource = ResourceSnapshot(
                resource_id=MCP_TOOL_CATALOG_RESOURCE_ID, resource_kind="mcp_tool_catalog",
                facet="activation", display_uri=None,
                revision=retained or "", payload=None,
                source_lineage=lineage, generation=publication.binding_ref.generation,
                available=False, error=publication.error, error_code="source-unavailable",
                retained_revision=retained, members=tuple(members),
            )
        definitions = []
        if publication.status == "available":
            for member in members:
                body = definitions_by_id[member.member_id].payload
                definitions.append(ResourceSnapshot(
                    resource_id=definition_resource_id(member.member_id), resource_kind="mcp_tool_catalog",
                    facet=DEFINITION_FACET, display_uri=None, revision=sha256_jcs(body), payload=body,
                    source_lineage=lineage, generation=publication.binding_ref.generation,
                    members=(member,),
                ))
        else:
            for resource_id in self._definition_ids:
                previous = self._registry.last_valid_snapshot(resource_id)
                definitions.append(ResourceSnapshot(
                    resource_id=resource_id, resource_kind="mcp_tool_catalog", facet=DEFINITION_FACET,
                    display_uri=None, revision=previous.revision, payload=None,
                    source_lineage=lineage, generation=publication.binding_ref.generation,
                    available=False, error=publication.error, error_code="source-unavailable",
                    retained_revision=previous.revision, members=previous.members,
                ))
        # 构造全批后一次发布；完整定义与 guidance/members 不得被分次捕获。
        with self._registry.publication():
            current_ids = {definition.resource_id for definition in definitions}
            for resource_id in self._definition_ids - current_ids:
                self._registry.withdraw(resource_id)
            for definition in definitions:
                self._registry.register_descriptor(SemanticResourceDescriptor(
                    resource_id=definition.resource_id, resource_kind="mcp_tool_catalog",
                    facet=DEFINITION_FACET, display_uri=None,
                    source_ids=(MCP_OWNER_CATALOG_SOURCE_ID, CUSTOM_CATALOG_SOURCE_ID, EXTENSION_CATALOG_SOURCE_ID),
                ))
                self._registry.publish(definition)
            self._registry.publish(resource)
            self._definition_ids = current_ids


def extension_dispatch_from_activation(
    *, snapshot: ResourceActivationSnapshotRef, binding: ResourceProvenanceRef,
    payload: object, model_call_id: str,
) -> ExtensionDispatchBindingRef:
    """只由 Saver 恢复的冻结父行/typed members/lineage 重建独立 dispatch 值。"""
    lineage_id, revision = frozen_extension_catalog_revision(snapshot=snapshot, binding=binding, payload=payload)
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


def frozen_extension_catalog_revision(
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


__all__ = [
    "MCP_GUIDANCE_SOURCE_ID",
    "MCP_TOOL_CATALOG_RESOURCE_ID",
    "McpCatalogResourcePublisher",
    "extension_dispatch_from_activation",
    "frozen_extension_catalog_revision",
    "mcp_guidance_content",
]
