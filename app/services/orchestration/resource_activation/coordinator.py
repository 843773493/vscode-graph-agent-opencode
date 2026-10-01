"""ResourceActivationCoordinator：只消费 Registry，只调用唯一 Saver。

冻结发生在 model-call 安全边界：coordinator 读 Registry 的内存 published
snapshot，把它固化成 domain :class:`ResourceActivationSnapshotRef`，并把正文
写进受保护 body store。它不做 stat/scan/read/HTTP fetch/memory-provider
lookup，也不回退到当前 Registry 的旧 revision。

Turn 内不可漂移：active execution slot 只在取得 active slot 时冻结一次
policy 与全部 turn-bound binding；存在 model-call-bound kind 时，后续 model
call 生成 parent-linked 组合 snapshot，逐字节复用 turn-bound binding，只替换
model-call-bound binding。required resource 仍 dirty/gap/unavailable 时有界
等待后 fail closed。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.resource_activation import (
    ResourceActivationSnapshotRef,
    ResourceProvenanceRef,
    SourceLineageRef,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
)
from app.services.orchestration.resource_activation.contracts import (
    ACTIVATION_OWNER_SCOPE,
    ResourceActivationBodyStore,
    ResourceActivationPolicySnapshot,
    ResourceActivationSnapshotSaver,
)

# required resource 处于 dirty/gap/unavailable 时的有界等待预算（秒）。
# 轮询只等待 Registry 的内存发布，不触发任何源 I/O。
DEFAULT_ACTIVATION_WAIT_SECONDS = 5.0
DEFAULT_ACTIVATION_POLL_SECONDS = 0.02


class ResourceActivationError(RuntimeError):
    """activation 冻结失败；调用方必须把它作为显式 dispatch 阻断处理。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code


def _registry_generation(snapshots) -> int:
    return max((item.generation for item in snapshots), default=0)


def _captured_at() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _snapshot_id(
    *,
    snapshot_kind: str,
    owner_session_id: str,
    owner_thread_id: str,
    turn_id: str,
    model_call_id: str | None,
) -> str:
    if snapshot_kind == "turn":
        return f"activation-turn:{owner_session_id}:{owner_thread_id}:{turn_id}"
    return (
        f"activation-model-call:{owner_session_id}:{owner_thread_id}:{turn_id}:"
        f"{model_call_id}"
    )


class ResourceActivationCoordinator:
    """把 ResourceRegistry published snapshot 冻结成 domain activation snapshot。"""

    def __init__(
        self,
        *,
        registry: ResourceRegistry,
        saver: ResourceActivationSnapshotSaver,
        body_store: ResourceActivationBodyStore,
        wait_seconds: float = DEFAULT_ACTIVATION_WAIT_SECONDS,
        poll_seconds: float = DEFAULT_ACTIVATION_POLL_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._registry = registry
        self._saver = saver
        self._body_store = body_store
        self._wait_seconds = wait_seconds
        self._poll_seconds = poll_seconds
        self._sleep = sleep

    async def freeze_turn(
        self,
        *,
        owner_session_id: str,
        owner_thread_id: str,
        turn_id: str,
        policy: ResourceActivationPolicySnapshot,
        checkpoint_ns: str = "",
    ) -> ResourceActivationSnapshotRef:
        """在 active execution slot 冻结 policy 和全部 turn-bound binding。"""

        self._validate_policy_kinds(policy)
        snapshots = await self._await_required_snapshots()
        snapshot = self._build_snapshot(
            snapshot_kind="turn",
            owner_session_id=owner_session_id,
            owner_thread_id=owner_thread_id,
            turn_id=turn_id,
            policy=policy,
            snapshots=snapshots,
            parent=None,
            model_call_id=None,
            checkpoint_ns=checkpoint_ns,
        )
        await self._saver.save_resource_activation_snapshot(snapshot)
        return snapshot

    async def prepare_model_call(
        self,
        *,
        parent: ResourceActivationSnapshotRef,
        model_call_id: str,
        policy: ResourceActivationPolicySnapshot | None = None,
        checkpoint_ns: str = "",
    ) -> ResourceActivationSnapshotRef:
        """没有 model_call kind 时复用 parent；否则建立 parent-linked snapshot。

        传入的 ``policy`` 必须与 parent 逐字节一致（Turn 内禁止 policy 漂移）；
        model-call binding 只允许追加，parent 的 turn-bound binding 逐字节复用。
        """

        if parent.snapshot_kind != "turn":
            raise ResourceActivationError(
                "resource-activation-parent-invalid",
                f"model call preparation 的 parent 必须是 turn snapshot: {parent.snapshot_kind}",
            )
        if policy is not None and (
            policy.revision != parent.activation_policy_revision
            or policy.hash != parent.activation_policy_hash
        ):
            raise ResourceActivationError(
                "resource-activation-policy-drift",
                "Turn 内 activation policy 不得漂移；新 policy 只对下一个 Turn 生效",
            )
        # policy revision/hash 是 boundaries 的 JCS 派生值：同一 revision/hash
        # 必然对应同一 boundaries，因此可以安全复用 caller 冻结的 policy。
        turn_policy = policy if policy is not None else _policy_from_snapshot(parent)
        if not turn_policy.has_model_call_boundary():
            return parent
        snapshots = await self._await_required_snapshots()
        snapshot = self._build_snapshot(
            snapshot_kind="model_call",
            owner_session_id=parent.owner_session_id,
            owner_thread_id=parent.owner_thread_id,
            turn_id=parent.turn_id,
            policy=turn_policy,
            snapshots=snapshots,
            parent=parent,
            model_call_id=model_call_id,
            checkpoint_ns=checkpoint_ns,
            reuse_parent_bindings=parent.bindings,
        )
        if len(snapshot.bindings) == len(parent.bindings):
            return parent
        await self._saver.save_resource_activation_snapshot(snapshot)
        return snapshot

    def _validate_policy_kinds(self, policy: ResourceActivationPolicySnapshot) -> None:
        registered_kinds = self._registry.resource_kinds()
        unknown = set(policy.boundaries) - registered_kinds
        if unknown:
            raise ResourceActivationError(
                "unknown-resource-kind",
                f"activation override 指向未登记 resource kind: {sorted(unknown)}",
            )

    async def _await_required_snapshots(self):
        """required resource 仍 dirty/gap/unavailable 时有界等待后 fail closed。"""

        deadline = time.monotonic() + self._wait_seconds
        while True:
            snapshots = self._registry.snapshots()
            if snapshots:
                unavailable = sorted(
                    item.resource_id for item in snapshots if not item.available
                )
                if not unavailable:
                    return snapshots
            if time.monotonic() >= deadline:
                if not snapshots:
                    raise ResourceActivationError(
                        "resource-registry-empty",
                        "ResourceRegistry 没有 published snapshot，不能冻结 activation",
                    )
                raise ResourceActivationError(
                    "resource-unavailable",
                    "ResourceRegistry 存在不可用 required snapshot: "
                    f"{unavailable}",
                )
            await self._sleep(self._poll_seconds)

    def _build_snapshot(
        self,
        *,
        snapshot_kind: str,
        owner_session_id: str,
        owner_thread_id: str,
        turn_id: str,
        policy: ResourceActivationPolicySnapshot,
        snapshots,
        parent: ResourceActivationSnapshotRef | None,
        model_call_id: str | None,
        checkpoint_ns: str,
        reuse_parent_bindings: tuple[ResourceProvenanceRef, ...] = (),
    ) -> ResourceActivationSnapshotRef:
        snapshot_id = _snapshot_id(
            snapshot_kind=snapshot_kind,
            owner_session_id=owner_session_id,
            owner_thread_id=owner_thread_id,
            turn_id=turn_id,
            model_call_id=model_call_id,
        )
        new_bindings: list[ResourceProvenanceRef] = list(reuse_parent_bindings)
        ordinal = len(reuse_parent_bindings)
        for item in sorted(snapshots, key=lambda entry: entry.resource_id):
            boundary = policy.effective_boundary(item.resource_kind)
            if snapshot_kind == "turn" and boundary != "turn":
                continue
            if snapshot_kind == "model_call" and boundary != "model_call":
                continue
            if not item.available:
                raise ResourceActivationError(
                    "resource-unavailable",
                    f"required resource 不可用: {item.resource_id}",
                )
            detail_ref = self._body_store.write_resource_body(
                owner_session_id=owner_session_id,
                activation_snapshot_id=snapshot_id,
                resource_id=item.resource_id,
                activation_ordinal=ordinal,
                payload=item.payload,
                checkpoint_ns=checkpoint_ns,
            )
            new_bindings.append(
                ResourceProvenanceRef(
                    resource_id=item.resource_id,
                    display_uri=item.display_uri,
                    resource_kind=item.resource_kind,
                    owner_scope=ACTIVATION_OWNER_SCOPE,
                    facet=item.facet,
                    revision=item.revision,
                    availability="available",
                    content_length=len(canonical_json_bytes(item.payload)),
                    content_hash=sha256_jcs(item.payload),
                    redacted_stable_digest=None,
                    source_lineage_ref=SourceLineageRef(
                        lineage_id=f"lineage:{item.resource_id}",
                        derivation_version="resource-derivation:v1",
                        sources=tuple(sorted(item.source_lineage)),
                    ),
                    source_lineage_digest=SourceLineageRef(
                        lineage_id=f"lineage:{item.resource_id}",
                        derivation_version="resource-derivation:v1",
                        sources=tuple(sorted(item.source_lineage)),
                    ).digest,
                    activation_ordinal=ordinal,
                    effective_boundary=boundary,
                    captured_registry_generation=item.generation,
                    detail_ref=detail_ref,
                )
            )
            ordinal += 1
        if not new_bindings:
            raise ResourceActivationError(
                "resource-registry-empty",
                "冻结 activation 至少需要一个 binding",
            )
        return ResourceActivationSnapshotRef(
            activation_snapshot_id=snapshot_id,
            snapshot_kind=snapshot_kind,
            activation_policy_revision=policy.revision,
            activation_policy_hash=policy.hash,
            registry_generation=max(
                _registry_generation(snapshots),
                0 if parent is None else parent.registry_generation,
            ),
            owner_session_id=owner_session_id,
            owner_thread_id=owner_thread_id,
            turn_id=turn_id,
            captured_at=_captured_at(),
            bindings=tuple(new_bindings),
            parent=parent,
            model_call_id=model_call_id,
        )


def _policy_from_snapshot(
    snapshot: ResourceActivationSnapshotRef,
) -> ResourceActivationPolicySnapshot:
    """从已冻结 snapshot 还原 policy 关系；不读取当前配置。

    冻结件只保存 policy revision/hash 与逐 binding 的 effective boundary。
    model_call 决策只需要知道「是否存在 model-call kind」，这由已冻结 binding
    与 policy hash 唯一决定，无需重读配置。
    """

    boundaries = {}
    for binding in snapshot.bindings:
        boundaries[binding.resource_kind] = binding.effective_boundary
    return ResourceActivationPolicySnapshot(
        revision=snapshot.activation_policy_revision,
        hash=snapshot.activation_policy_hash,
        default_boundary="turn",
        boundaries=boundaries,
    )


__all__ = [
    "DEFAULT_ACTIVATION_POLL_SECONDS",
    "DEFAULT_ACTIVATION_WAIT_SECONDS",
    "ResourceActivationCoordinator",
    "ResourceActivationError",
]
