"""ResourceActivationCoordinator：只消费 Registry，只调用唯一 Saver。"""

from __future__ import annotations

from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.orchestration.resource_activation.contracts import (
    ModelCallResourceSnapshot,
    ResourceActivationBinding,
    ResourceActivationPolicySnapshot,
    ResourceActivationSnapshotSaver,
    TurnResourceSnapshot,
)


class ResourceActivationError(RuntimeError):
    """activation 冻结失败；调用方必须把它作为显式 dispatch 阻断处理。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code


class ResourceActivationCoordinator:
    """把 ResourceRegistry published snapshot 冻结成 Turn/ModelCall snapshot。"""

    def __init__(
        self,
        *,
        registry: ResourceRegistry,
        saver: ResourceActivationSnapshotSaver,
    ) -> None:
        self._registry = registry
        self._saver = saver

    async def freeze_turn(
        self,
        *,
        owner_session_id: str,
        owner_thread_id: str,
        turn_id: str,
        policy: ResourceActivationPolicySnapshot,
    ) -> TurnResourceSnapshot:
        """在 active execution slot 冻结 policy 和全部 turn-bound binding。"""

        self._validate_policy_kinds(policy)
        snapshots = self._available_snapshots()
        turn_bindings = tuple(
            ResourceActivationBinding.from_resource_snapshot(
                snapshot,
                effective_boundary=policy.effective_boundary(snapshot.resource_kind),
            )
            for snapshot in sorted(snapshots, key=lambda item: item.resource_id)
            if policy.effective_boundary(snapshot.resource_kind) == "turn"
        )
        snapshot = TurnResourceSnapshot(
            activation_snapshot_id=(
                f"turn:{owner_session_id}:{owner_thread_id}:{turn_id}"
            ),
            snapshot_kind="turn",
            policy=policy,
            owner_session_id=owner_session_id,
            owner_thread_id=owner_thread_id,
            turn_id=turn_id,
            registry_generation=max(item.generation for item in snapshots),
            bindings=turn_bindings,
        )
        await self._saver.save_resource_activation_snapshot(snapshot)
        return snapshot

    async def prepare_model_call(
        self,
        *,
        parent: TurnResourceSnapshot,
        model_call_id: str,
    ) -> TurnResourceSnapshot | ModelCallResourceSnapshot:
        """没有 model_call kind 时复用 parent；否则建立 parent-linked snapshot。"""

        if not parent.policy.has_model_call_boundary():
            return parent
        snapshots = self._available_snapshots()
        parent_boundaries = {
            binding.resource_id: binding.effective_boundary
            for binding in parent.bindings
        }
        model_call_bindings = tuple(
            ResourceActivationBinding.from_resource_snapshot(
                snapshot,
                effective_boundary=parent.policy.effective_boundary(
                    snapshot.resource_kind
                ),
            )
            for snapshot in sorted(snapshots, key=lambda item: item.resource_id)
            if snapshot.resource_id not in parent_boundaries
            and parent.policy.effective_boundary(snapshot.resource_kind) == "model_call"
        )
        if not model_call_bindings:
            return parent
        snapshot = ModelCallResourceSnapshot(
            parent=parent,
            model_call_id=model_call_id,
            snapshot_kind="model_call",
            registry_generation=max(
                parent.registry_generation,
                *(item.generation for item in snapshots),
            ),
            bindings=(*parent.bindings, *model_call_bindings),
        )
        await self._saver.save_resource_activation_snapshot(snapshot)
        return snapshot

    def _validate_policy_kinds(
        self, policy: ResourceActivationPolicySnapshot
    ) -> None:
        registered_kinds = self._registry.resource_kinds()
        unknown = set(policy.boundaries) - registered_kinds
        if unknown:
            raise ResourceActivationError(
                "unknown-resource-kind",
                f"activation override 指向未登记 resource kind: {sorted(unknown)}",
            )

    def _available_snapshots(self):
        snapshots = self._registry.snapshots()
        if not snapshots:
            raise ResourceActivationError(
                "resource-registry-empty",
                "ResourceRegistry 没有 published snapshot，不能冻结 activation",
            )
        unavailable = [item.resource_id for item in snapshots if not item.available]
        if unavailable:
            raise ResourceActivationError(
                "resource-unavailable",
                f"ResourceRegistry 存在不可用 required snapshot: {sorted(unavailable)}",
            )
        return snapshots
