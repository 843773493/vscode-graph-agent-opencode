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
from dataclasses import replace
from typing import Final

from app.domain.itemized.errors import CodeCarryingError
from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.hashing import canonical_json_bytes, sha256_jcs
from app.domain.itemized.resource_activation import (
    ResourceActivationSnapshotRef,
    ResourceProvenanceRef,
    SourceLineageRef,
)
from app.services.infrastructure.resource_platform.derivation.types import (
    ResourceSnapshot,
)
from app.services.infrastructure.resource_platform.registry.semantic_registry import (
    ResourceRegistry,
    ResourceRegistryCapture,
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

# 冻结失败码：只在此定义一次，杜绝同一 code 在多处裸写漂移。
_CODE_PARENT_INVALID: Final = "resource-activation-parent-invalid"
_CODE_POLICY_DRIFT: Final = "resource-activation-policy-drift"


class ResourceActivationError(CodeCarryingError, RuntimeError):
    """activation 冻结失败；调用方必须把它作为显式 dispatch 阻断处理。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


def _registry_generation(snapshots: tuple[ResourceSnapshot, ...]) -> int:
    return max((item.generation for item in snapshots), default=0)


def _captured_at() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _snapshot_id(
    *,
    snapshot_kind: str,
    owner_session_id: str,
    owner_thread_id: str,
    execution_id: str,
    model_call_id: str | None,
) -> str:
    if snapshot_kind == "execution":
        return f"activation-execution:{owner_session_id}:{owner_thread_id}:{execution_id}"
    return (
        f"activation-model-call:{owner_session_id}:{owner_thread_id}:{execution_id}:"
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

    async def freeze_execution(
        self,
        *,
        admission: ExecutionAdmission,
        policy: ResourceActivationPolicySnapshot,
        checkpoint_ns: str = "",
    ) -> ResourceActivationSnapshotRef:
        """active slot 先恢复唯一冻结事实；同用户 Turn retry 只建立新 execution 关联。"""

        if not isinstance(admission, ExecutionAdmission):
            raise TypeError("freeze_execution 必须使用真实 typed admission")
        frozen = self._saver.frozen_resource_activation_for_admission(
            admission, checkpoint_ns=checkpoint_ns
        )
        if frozen is not None:
            if (
                frozen.snapshot_kind != "execution"
                or frozen.owner_session_id != admission.session_id
                or frozen.owner_thread_id != admission.thread_id
                or frozen.turn_id != admission.turn_id
            ):
                raise ResourceActivationError(
                    _CODE_PARENT_INVALID, "冻结 snapshot 与真实 admission owner 不一致"
                )
            if frozen.execution_id == admission.execution_id:
                return frozen
            if admission.execution_origin != "user_turn":
                raise ResourceActivationError(
                    _CODE_PARENT_INVALID, "internal execution 不得借用其它 execution 冻结事实"
                )
            snapshot = replace(
                frozen,
                activation_snapshot_id=_snapshot_id(
                    snapshot_kind="execution",
                    owner_session_id=admission.session_id,
                    owner_thread_id=admission.thread_id,
                    execution_id=admission.execution_id,
                    model_call_id=None,
                ),
                execution_id=admission.execution_id,
            )
        else:
            snapshots = await self._await_required_snapshots(policy, boundary="turn")
            snapshot = self._build_snapshot(
                snapshot_kind="execution",
                owner_session_id=admission.session_id,
                owner_thread_id=admission.thread_id,
                execution_id=admission.execution_id,
                turn_id=admission.turn_id,
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

        if parent.snapshot_kind != "execution":
            raise ResourceActivationError(
                _CODE_PARENT_INVALID,
                f"model call preparation 的 parent 必须是 execution snapshot: {parent.snapshot_kind}",
            )
        if policy is not None and (
            policy.revision != parent.activation_policy_revision
            or policy.hash != parent.activation_policy_hash
        ):
            raise ResourceActivationError(
                _CODE_POLICY_DRIFT,
                "Turn 内 activation policy 不得漂移；新 policy 只对下一个 Turn 生效",
            )
        frozen_policy = parent.activation_policy
        if not frozen_policy.has_model_call_boundary():
            return parent
        snapshot_id = _snapshot_id(
            snapshot_kind="model_call",
            owner_session_id=parent.owner_session_id,
            owner_thread_id=parent.owner_thread_id,
            execution_id=parent.execution_id,
            model_call_id=model_call_id,
        )
        frozen = self._saver.find_resource_activation_snapshot(
            parent.owner_session_id, thread_id=parent.owner_thread_id,
            activation_snapshot_id=snapshot_id, checkpoint_ns=checkpoint_ns,
        )
        if frozen is not None:
            if frozen.parent != parent or frozen.model_call_id != model_call_id:
                raise ResourceActivationError(
                    _CODE_PARENT_INVALID, "model_call 冻结事实与 execution parent 不一致"
                )
            return frozen
        snapshots = await self._await_required_snapshots(frozen_policy, boundary="model_call")
        snapshot = self._build_snapshot(
            snapshot_kind="model_call",
            owner_session_id=parent.owner_session_id,
            owner_thread_id=parent.owner_thread_id,
            execution_id=parent.execution_id,
            turn_id=parent.turn_id,
            policy=frozen_policy,
            snapshots=snapshots,
            parent=parent,
            model_call_id=model_call_id,
            checkpoint_ns=checkpoint_ns,
            reuse_parent_bindings=parent.bindings,
        )
        await self._saver.save_resource_activation_snapshot(snapshot)
        return snapshot

    def _validate_policy_kinds(
        self, policy: ResourceActivationPolicySnapshot, capture: ResourceRegistryCapture
    ) -> None:
        unknown = set(policy.boundaries) - capture.resource_kinds
        if unknown:
            raise ResourceActivationError(
                "unknown-resource-kind",
                f"activation override 指向未登记 resource kind: {sorted(unknown)}",
            )

    async def _await_required_snapshots(
        self, policy: ResourceActivationPolicySnapshot, *, boundary: str
    ) -> tuple[ResourceSnapshot, ...]:
        """只等当前边界必需的内存发布；descriptor 缺 snapshot 也必须阻断。"""

        deadline = time.monotonic() + self._wait_seconds
        while True:
            capture = self._registry.capture()
            self._validate_policy_kinds(policy, capture)
            snapshots = tuple(
                item for item in capture.snapshots
                if policy.effective_boundary(item.resource_kind) == boundary
            )
            required = {
                item.resource_id for item in capture.descriptors
                if policy.effective_boundary(item.resource_kind) == boundary
            }
            missing = required - {item.resource_id for item in snapshots}
            unavailable = {
                item.resource_id for item in snapshots if not item.available
            } | missing
            if not unavailable:
                return capture.snapshots
            if time.monotonic() >= deadline:
                raise ResourceActivationError(
                    "resource-unavailable",
                    f"ResourceRegistry required snapshot 缺失或不可用: {sorted(unavailable)}",
                )
            await self._sleep(self._poll_seconds)

    def _build_snapshot(
        self,
        *,
        snapshot_kind: str,
        owner_session_id: str,
        owner_thread_id: str,
        execution_id: str,
        turn_id: str | None,
        policy: ResourceActivationPolicySnapshot,
        snapshots: tuple[ResourceSnapshot, ...],
        parent: ResourceActivationSnapshotRef | None,
        model_call_id: str | None,
        checkpoint_ns: str,
        reuse_parent_bindings: tuple[ResourceProvenanceRef, ...] = (),
    ) -> ResourceActivationSnapshotRef:
        snapshot_id = _snapshot_id(
            snapshot_kind=snapshot_kind,
            owner_session_id=owner_session_id,
            owner_thread_id=owner_thread_id,
            execution_id=execution_id,
            model_call_id=model_call_id,
        )
        new_bindings: list[ResourceProvenanceRef] = list(reuse_parent_bindings)
        ordinal = len(reuse_parent_bindings)
        for item in sorted(snapshots, key=lambda entry: entry.resource_id):
            boundary = policy.effective_boundary(item.resource_kind)
            if snapshot_kind == "execution" and boundary != "turn":
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
                owner_thread_id=owner_thread_id,
                activation_snapshot_id=snapshot_id,
                resource_id=item.resource_id,
                activation_ordinal=ordinal,
                payload=item.payload,
                checkpoint_ns=checkpoint_ns,
            )
            lineage = SourceLineageRef(
                lineage_id=f"lineage:{item.resource_id}",
                derivation_version="resource-derivation:v1",
                sources=tuple(sorted(item.source_lineage)),
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
                    source_lineage_ref=lineage,
                    source_lineage_digest=lineage.digest,
                    activation_ordinal=ordinal,
                    effective_boundary=boundary,
                    captured_registry_generation=item.generation,
                    detail_ref=detail_ref,
                    members=item.members,
                )
            )
            ordinal += 1
        return ResourceActivationSnapshotRef(
            activation_snapshot_id=snapshot_id,
            snapshot_kind=snapshot_kind,
            activation_policy_revision=policy.revision,
            activation_policy_hash=policy.hash,
            activation_policy=policy,
            registry_generation=max(
                _registry_generation(snapshots),
                0 if parent is None else parent.registry_generation,
            ),
            owner_session_id=owner_session_id,
            owner_thread_id=owner_thread_id,
            execution_id=execution_id,
            turn_id=turn_id,
            captured_at=_captured_at(),
            bindings=tuple(new_bindings),
            parent=parent,
            model_call_id=model_call_id,
        )



__all__ = [
    "DEFAULT_ACTIVATION_POLL_SECONDS",
    "DEFAULT_ACTIVATION_WAIT_SECONDS",
    "ResourceActivationCoordinator",
    "ResourceActivationError",
]
