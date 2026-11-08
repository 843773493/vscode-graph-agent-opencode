"""MCP Owner publication 到同一 runtime ResourceRegistry 的纯内存适配。

父行 payload 只承载实际选中的工具指引正文；完整定义使用同批 definition facet。
目录 revision、generation 与 typed members 进入独立 lineage/member 通道；
冻结与持久化归既有 Coordinator/Saver。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from langchain_core.tools import BaseTool

from app.agents.policy.tool_capability import (
    ToolCapabilityPolicy,
    ToolMetadata,
    metadata_for_tool,
)
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.resource_activation import (
    ResourceActivationSnapshotRef,
    ResourceMemberBinding,
    ResourceProvenanceRef,
)
from app.services.infrastructure.mcp.catalog_owner import McpCatalogPublication
from app.services.infrastructure.mcp.extension_catalog import (
    ExtensionCatalogBindingError,
    ExtensionCatalogUnavailableError,
    ExtensionDispatchBindingRef,
    extension_catalog_binding_from_members,
    payload_digest,
)
from app.services.infrastructure.mcp.tool_definitions import (
    DEFINITION_FACET,
    SkillToolAssociation,
    SkillToolSource,
    ToolDefinition,
    _tool_skill_associations,
    definition_from_tool,
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
SKILL_TOOL_ASSOCIATION_SOURCE_ID = "skill-tool-associations:v1"


def mcp_guidance_content(payload: object) -> str:
    """资源正文严格只包含选中的 guidance，不容纳成员或 provenance。"""
    if not isinstance(payload, Mapping) or set(payload) != {"content"} or not isinstance(payload["content"], str):
        raise ExtensionCatalogUnavailableError("mcp_tool_catalog protected payload 不是完整的 guidance 正文")
    return payload["content"]


def frozen_skill_tool_associations(payload: object) -> tuple[SkillToolAssociation, ...]:
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


class McpCatalogResourcePublisher:
    """无 I/O、无独立 catalog，只把唯一 Owner 的完整值发布到调用方 Registry。"""

    def __init__(
        self, *, registry: ResourceRegistry, custom_tools: Sequence[BaseTool] = (),
        policy_resolver: Callable[[Sequence[ToolMetadata]], Mapping[str, ToolCapabilityPolicy]],
        custom_metadata: Mapping[str, ToolMetadata] | None = None,
        custom_skill_bindings: Mapping[str, Sequence[str]] | None = None,
        skill_sources: Sequence[SkillToolSource] = (),
        gateway_id: str | None = None,
    ) -> None:
        self._policy_resolver = policy_resolver
        self._registry = registry
        self._custom_skill_bindings = {key: tuple(value) for key, value in (custom_skill_bindings or {}).items()}
        self._skill_sources = tuple(skill_sources)
        self._gateway_id = gateway_id
        entries = tuple(sorted(custom_tools, key=lambda tool: tool.name))
        metadata = {tool.name: (custom_metadata or {}).get(tool.name) or metadata_for_tool(tool, origin="custom")
                    for tool in entries}
        self._custom_definitions: dict[str, ToolDefinition] = {}
        for tool in entries:
            self._custom_definitions[tool.name] = definition_from_tool(tool, metadata[tool.name])
        self._definition_ids: set[str] = set()
        self._custom_revision = payload_digest(
            [[name, definition.payload] for name, definition in self._custom_definitions.items()],
            context="工作区 custom extension typed members",
            error_type=ExtensionCatalogBindingError,
        )
        # 定义已冻结完整 schema 摘要；只构造成员，不再解码、哈希 schema 或构造随即丢弃的 binding。
        self._custom_members = tuple(
            ResourceMemberBinding(
                member_id=name, member_kind="custom", schema_hash=definition.schema_hash,
                server_id=None, revision=self._custom_revision,
            )
            for name, definition in self._custom_definitions.items()
        )
        registry.register_descriptor(SemanticResourceDescriptor(
            resource_id=MCP_TOOL_CATALOG_RESOURCE_ID, resource_kind="mcp_tool_catalog",
            facet="activation", display_uri=None,
            source_ids=(MCP_OWNER_CATALOG_SOURCE_ID, CUSTOM_CATALOG_SOURCE_ID,
                        SKILL_TOOL_ASSOCIATION_SOURCE_ID, EXTENSION_CATALOG_SOURCE_ID),
        ))

    def publish(self, publication: McpCatalogPublication) -> None:
        if not isinstance(publication, McpCatalogPublication):
            raise TypeError("MCP resource publisher 需要真实 Owner publication")
        definitions_by_id = {**publication.definitions, **self._custom_definitions}
        definitions_by_id = {
            tool_id: replace(
                definition,
                skill_associations=_tool_skill_associations(
                    tool_id, definition, self._skill_sources,
                    self._custom_skill_bindings.get(tool_id, ()),
                    gateway_id=self._gateway_id,
                ),
            )
            for tool_id, definition in definitions_by_id.items()
        }
        metadata = {name: definition.metadata for name, definition in definitions_by_id.items()}
        policies = self._policy_resolver(tuple(metadata.values()))
        if set(policies) != set(definitions_by_id):
            raise ValueError("扩展目录发布需要同一次选择快照的完整有效策略")
        # 冻结目录保存已知定义事实；策略只裁决展示与执行，不把禁用伪装成不存在。
        members = (*publication.binding_ref.members, *self._custom_members)
        missing_tool_associations = [
            SkillToolAssociation(
                skill_name=source.name, tool_id=binding.tool_id,
                source_id=source.source_id, entry_identity=source.entry_identity,
                display_uri=source.display_uri, status="unavailable",
                reason=("skill-source-not-effective" if not source.effective else "tool-not-in-published-catalog"),
            )
            for source in self._skill_sources if source.plugin is not None
            for binding in source.plugin.tool_bindings
            if binding.tool_id not in definitions_by_id
        ]
        effective_skills = {source.name: source for source in self._skill_sources if source.effective}
        for tool_id, names in self._custom_skill_bindings.items():
            if tool_id in definitions_by_id:
                continue
            for name in names:
                source = effective_skills.get(name)
                missing_tool_associations.append(SkillToolAssociation(
                    skill_name=name, tool_id=tool_id,
                    source_id=None if source is None else source.source_id,
                    entry_identity=None if source is None else source.entry_identity,
                    display_uri=None if source is None else source.display_uri,
                    status="unavailable",
                    reason="skill-not-in-published-catalog" if source is None else "tool-not-in-published-catalog",
                ))
        missing_tool_associations = tuple({
            (item.tool_id, item.skill_name, item.entry_identity): item
            for item in missing_tool_associations
        }.values())
        association_values = tuple(
            association for definition in definitions_by_id.values()
            for association in definition.skill_associations
        ) + missing_tool_associations
        association_revision = payload_digest(
            [item.to_dict() for item in association_values],
            context="Skill 工具来源关联",
            error_type=ExtensionCatalogBindingError,
        )
        revision = payload_digest(
            [publication.binding_ref.catalog_revision, self._custom_revision, association_revision],
            context="唯一扩展目录 lineage",
            error_type=ExtensionCatalogBindingError,
        )
        lineage = (
            (MCP_OWNER_CATALOG_SOURCE_ID, publication.binding_ref.catalog_revision),
            (CUSTOM_CATALOG_SOURCE_ID, self._custom_revision),
            (SKILL_TOOL_ASSOCIATION_SOURCE_ID, association_revision),
            (EXTENSION_CATALOG_SOURCE_ID, revision),
        )
        if publication.status == "available":
            payload = {"content": extension_tool_guidance(
                tuple(definitions_by_id.values()),
                {name: policies[name] for name in definitions_by_id},
                skill_associations=missing_tool_associations,
            )}
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
                body = definitions_by_id[member.member_id].activation_payload()
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
