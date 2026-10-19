"""把本次 Execution/ModelCall 冻结的 AGENTS 语义链交给唯一 CSM owner。"""

from __future__ import annotations

from collections.abc import Callable

from app.domain.itemized.resource_activation import (
    ResourceActivationSnapshotRef,
    ResourceProvenanceRef,
)
from app.services.business.resource_derivations.agents import (
    WORKSPACE_AGENTS_RESOURCE_ID,
    WORKSPACE_AGENTS_SOURCE_ID,
    agents_content,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
    ContextSourceDescriptor,
)


def _chain_binding(activation: ResourceActivationSnapshotRef) -> ResourceProvenanceRef | None:
    bindings = tuple(binding for binding in activation.bindings if binding.resource_id == WORKSPACE_AGENTS_RESOURCE_ID)
    if not bindings:
        return None
    if len(bindings) != 1:
        raise ValueError("resource-activation-snapshot-conflict: AGENTS chain binding 不唯一")
    binding = bindings[0]
    if (binding.resource_kind, binding.facet, binding.display_uri, binding.availability) != (
        "workspace_agents", "chain", None, "available",
    ):
        raise ValueError("resource-activation-snapshot-conflict: AGENTS chain binding 非法")
    return binding


def install_frozen_workspace_agents(
    manager: ContextSourceManager,
    activation: ResourceActivationSnapshotRef,
    read_payload: Callable[[ResourceActivationSnapshotRef, ResourceProvenanceRef], object],
    read_committed_activation: Callable[[], ResourceActivationSnapshotRef],
) -> None:
    """只读已封存 detail；来源 observation/applied 继续归原 CSM/Saver 事务。"""
    binding = _chain_binding(activation)
    if binding is None:
        if any(source.source_id == WORKSPACE_AGENTS_SOURCE_ID for source in manager.descriptors()):
            raise RuntimeError("resource-activation-snapshot-conflict: 冻结快照缺少 AGENTS chain")
        return
    content = agents_content(read_payload(activation, binding))
    manager.register(ContextSourceDescriptor(
        source_id=WORKSPACE_AGENTS_SOURCE_ID, source_kind="workspace_agents", name="AGENTS.md",
        description="当前工作区已登记的 AGENTS 指令链", internal_locator=WORKSPACE_AGENTS_RESOURCE_ID,
        root_placement="root_eligible",
    ), tracking_status="tracked")

    def read_committed_content(revision: str) -> str:
        frozen = read_committed_activation()
        committed = _chain_binding(frozen)
        if committed is None or committed.revision != revision:
            raise RuntimeError("resource-activation-snapshot-conflict: 已 applied AGENTS chain 与前驱不一致")
        return agents_content(read_payload(frozen, committed))

    manager.restore_committed_baseline(WORKSPACE_AGENTS_SOURCE_ID, read_committed_content)
    manager.observe(WORKSPACE_AGENTS_SOURCE_ID, content, revision=binding.revision)
