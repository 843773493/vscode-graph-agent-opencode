"""运行时编译时装配 AGENTS 来源，发布阶段只消费原 owner 的 accepted revision。"""

from __future__ import annotations

from app.core.resource_uri import parse_resource_uri
from app.services.business.resource_derivations.agents import (
    WORKSPACE_AGENTS_RESOURCE_ID,
    AgentsChainLoader,
    AgentsFileLoader,
)
from app.services.infrastructure.resource_platform.derivation.graph import (
    ResourceDerivationGraph,
)
from app.services.infrastructure.resource_platform.derivation.types import (
    ResourceSnapshot,
    SemanticResourceDescriptor,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.infrastructure.resource_platform.sources.workspace_file_resources import (
    WorkspaceFileResourceRegistry,
)
from app.services.infrastructure.resource_platform.virtual_resources import (
    workspace_agent_spec_display_uri,
)


def register_workspace_agents_source(
    file_registry: WorkspaceFileResourceRegistry, *, workspace_id: str,
) -> tuple[str, ...]:
    """只登记工作区已知根文件；首次读取属于 runtime 编译而非模型请求。"""
    uri = workspace_agent_spec_display_uri(workspace_id)
    file_registry.register_file(
        uri=uri,
        path=file_registry.workspace_root / "AGENTS.md",
        source_id=f"workspace-agents-source:{workspace_id}",
        entry_identity=f"workspace-agents-entry:{workspace_id}",
        optional=True,
    )
    return (uri,)


def publish_workspace_agents(
    *, registry: ResourceRegistry, file_registry: WorkspaceFileResourceRegistry,
    file_uris: tuple[str, ...],
) -> ResourceSnapshot:
    """已登记文件 → raw-file facets → 有序 chain；未登记 URI 在图构建前明确拒绝。"""
    if not file_uris or len(set(file_uris)) != len(file_uris):
        raise ValueError("AGENTS 链必须包含不重复的已登记来源")
    identities = tuple(parse_resource_uri(uri) for uri in file_uris)
    if any(identity.scope != "workspace" or identity.scope_id != identities[0].scope_id for identity in identities):
        raise ValueError("AGENTS 链只允许同一 Workspace owner 的已登记来源")
    source_ids = tuple(file_registry.source_id_for_uri(uri) for uri in file_uris)
    with registry.publication():
        graph = ResourceDerivationGraph(reconciler=file_registry, registry=registry)
        raw_ids = []
        for uri, source_id in zip(file_uris, source_ids, strict=True):
            resource_id = "workspace-agents:raw-file:" + source_id
            raw_ids.append(resource_id)
            graph.register(SemanticResourceDescriptor(
                resource_id=resource_id, resource_kind="workspace_agents", facet="raw-file",
                display_uri=uri, source_ids=(source_id,), loader_version=AgentsFileLoader.version,
            ), AgentsFileLoader(source_id))
        for descriptor in registry.descriptors():
            if descriptor.resource_kind == "workspace_agents" and descriptor.facet == "raw-file" and descriptor.resource_id not in raw_ids:
                registry.withdraw(descriptor.resource_id)
        try:
            previous = registry.descriptor(WORKSPACE_AGENTS_RESOURCE_ID)
        except KeyError:
            previous = None
        graph.register(SemanticResourceDescriptor(
            resource_id=WORKSPACE_AGENTS_RESOURCE_ID, resource_kind="workspace_agents", facet="chain",
            display_uri=None, dependency_resource_ids=tuple(raw_ids), loader_version=AgentsChainLoader.version,
        ), AgentsChainLoader(tuple(raw_ids)), expected=previous)
        graph.publish_all()
        return registry.snapshot(WORKSPACE_AGENTS_RESOURCE_ID)
