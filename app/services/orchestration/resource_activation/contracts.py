"""资源激活冻结边界的 policy 与唯一持久化 port。

冻结快照本体只有一个事实源：domain 的
:class:`app.domain.itemized.resource_activation.ResourceActivationSnapshotRef`
（9.1 冻结）。本模块只负责有效配置选择、受保护 body port 和唯一 Saver port，
完整冻结 policy 与 Execution/ModelCall snapshot 类型由领域合同定义。
"""

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar, Token
from typing import Protocol

from app.domain.itemized.execution_admission import ExecutionAdmission
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.resource_activation import (
    ResourceActivationPolicySnapshot,
    ResourceActivationSnapshotRef,
)
from app.domain.itemized.resource_activation.common import ResourceActivationBoundary

# binding 的 owner scope 由资源平台声明；derivation 契约当前未携带该字段
# （见 derivation/types.SemanticResourceDescriptor）。在资源平台补全前，
# activation 统一使用 session-scoped 语义，不得由 URI/路径推断。
# TODO(9.3→resource_platform): ResourceSnapshot 增加 owner_scope 后改为透传。
ACTIVATION_OWNER_SCOPE = "session"


def resource_activation_policy_from_config(
    config: Mapping[str, object],
) -> ResourceActivationPolicySnapshot:
    """从有效配置选择 activation policy；领域类型只持有冻结后的纯值。"""

    context = config.get("context")
    if not isinstance(context, Mapping):
        raise TypeError("context 配置必须是对象")
    return ResourceActivationPolicySnapshot.from_dict(context.get("resource_activation"))


_current_execution_activation: ContextVar[ResourceActivationSnapshotRef | None] = ContextVar(
    "current_execution_resource_activation", default=None,
)


def _execution_activation_matches(
    snapshot: ResourceActivationSnapshotRef, admission: ExecutionAdmission,
) -> None:
    if snapshot.snapshot_kind != "execution" or (
        snapshot.owner_session_id, snapshot.owner_thread_id,
        snapshot.execution_id, snapshot.turn_id,
    ) != (
        admission.session_id, admission.thread_id, admission.execution_id, admission.turn_id,
    ):
        raise ValueError("resource-activation-snapshot-conflict: 冻结 root 与当前 admission 不一致")


def set_current_execution_resource_activation(
    snapshot: ResourceActivationSnapshotRef, *, admission: ExecutionAdmission,
) -> Token[ResourceActivationSnapshotRef | None]:
    """仅向子调用传递 Saver 已持久化的不可变 root，不承担恢复或缓存权威。"""

    if not isinstance(snapshot, ResourceActivationSnapshotRef):
        raise TypeError("execution activation 必须是 typed snapshot")
    _execution_activation_matches(snapshot, admission)
    return _current_execution_activation.set(snapshot)


def reset_current_execution_resource_activation(
    token: Token[ResourceActivationSnapshotRef | None],
) -> None:
    _current_execution_activation.reset(token)


def require_current_execution_resource_activation(
    admission: ExecutionAdmission,
) -> ResourceActivationSnapshotRef:
    """实际模型调用必须消费当前 execution 的已冻结 root。"""

    snapshot = _current_execution_activation.get()
    if snapshot is None:
        raise RuntimeError("resource-activation-unavailable: 当前 execution 尚未冻结 root")
    _execution_activation_matches(snapshot, admission)
    return snapshot


class ResourceActivationBodyStore(Protocol):
    """把已冻结的语义 payload 正文写入受保护 detail/snapshot body store。

    只接受 activation coordinator 传入的内存 payload；绝不读取当前文件、
    URI、网络或 memory provider。返回 target-local typed :class:`DetailRef`。
    """

    def write_resource_body(
        self,
        *,
        owner_session_id: str,
        owner_thread_id: str,
        activation_snapshot_id: str,
        resource_id: str,
        activation_ordinal: int,
        payload: object,
        checkpoint_ns: str,
    ) -> DetailRef: ...


class ResourceActivationSnapshotSaver(Protocol):
    """itemized rollout owner 暴露给本目录的唯一持久化 port。

    只接受 domain 已冻结的内存 snapshot；失败必须抛出，不允许 Coordinator
    伪造成功。seal 路径另外通过 Saver 的 assembly 事务原子绑定。
    """

    def context_owner_namespace(self, checkpoint_ns: str) -> str: ...

    def frozen_resource_activation_for_admission(
        self, admission: ExecutionAdmission, *, checkpoint_ns: str = ""
    ) -> ResourceActivationSnapshotRef | None: ...

    def find_resource_activation_snapshot(
        self,
        session_id: str,
        *,
        thread_id: str,
        activation_snapshot_id: str,
        checkpoint_ns: str = "",
    ) -> ResourceActivationSnapshotRef | None: ...

    async def save_resource_activation_snapshot(
        self, snapshot: ResourceActivationSnapshotRef, *, checkpoint_ns: str = ""
    ) -> None: ...


__all__ = [
    "ACTIVATION_OWNER_SCOPE",
    "ResourceActivationBodyStore",
    "ResourceActivationBoundary",
    "ResourceActivationPolicySnapshot",
    "ResourceActivationSnapshotSaver",
    "require_current_execution_resource_activation",
    "reset_current_execution_resource_activation",
    "resource_activation_policy_from_config",
    "set_current_execution_resource_activation",
]
