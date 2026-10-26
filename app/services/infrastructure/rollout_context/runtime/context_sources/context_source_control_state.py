"""CSM 控制状态的 typed 结构与只读/写端口合同。

本模块只定义值对象和 ``Protocol``，不执行任何 I/O。``ContextSourceManager``
只通过该端口读写自己的控制状态；SQLite 表、owner 锁和事务边界由唯一的
``RolloutCheckpointSaver``/ContextStore owner 实现。

控制状态按精确 ``(session_id, thread_id)`` 归属：所有 Thread（包括 main）都
必须使用 catalog 中的 canonical Thread ID。它只保存
稳定 source identity、catalog/来源绑定 revision、observed/applied revision
和 untrack（frozen）状态；locator、handle 和正文归 Registry/实际 source
owner 所有，并在重启后由它们重建，不进入本状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.epoch.prefix_epoch import PendingPrefixEpochTransition
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
    _normalize_skill_name,
)

ContextSourceTrackingStatus = Literal["tracked", "untracked"]
"""``untracked`` 即 OpenSpec 的 frozen 状态：停止消费 revision 且不再恢复。"""


@dataclass(frozen=True, slots=True)
class ContextSourceOwnerKey:
    """CSM 控制状态的 owner：一个精确的 SessionThread。"""

    session_id: str
    thread_id: str

    def __post_init__(self) -> None:
        for field_name in ("session_id", "thread_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"ContextSourceOwnerKey.{field_name} 必须是非空字符串")


@dataclass(frozen=True, slots=True)
class ContextSourceControlState:
    """一个 tracked registration 的持久化控制状态。

    ``binding_revision`` 表示目录/来源绑定版本；``latest_revision`` 是来源
    owner 最近观察到的候选内容版本，不保证已经对上下文可见。
    ``latest_visible_committed_revision`` 是成功 seal 事务推进、由 receipt
    确认的上下文可见版本；晚到的 desired 版本仍可处于 pending。内容 revision
    允许 source owner 显式给出非空字符串，不承诺递增数字或固定 hash。

    ``state_revision`` 是持久控制行的 CAS 版本：每次真正改变控制字段的
    写入都会 +1，用于乐观冲突检测，与绑定版本和内容版本分开。
    """

    owner: ContextSourceOwnerKey
    source_id: str
    source_kind: str
    name: str
    binding_revision: str | None
    tracking_status: ContextSourceTrackingStatus
    latest_visible_committed_revision: str | None
    latest_revision: str | None
    state_revision: int = 0
    updated_at: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.owner, ContextSourceOwnerKey):
            raise TypeError(
                "ContextSourceControlState.owner 必须是 ContextSourceOwnerKey"
            )
        for field_name in ("source_id", "source_kind", "name"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"ContextSourceControlState.{field_name} 必须是非空字符串"
                )
        for field_name in (
            "binding_revision",
            "latest_visible_committed_revision",
            "latest_revision",
        ):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(
                    f"ContextSourceControlState.{field_name} 必须是非空字符串或 null"
                )
        if self.tracking_status not in {"tracked", "untracked"}:
            raise ValueError(
                "ContextSourceControlState.tracking_status 只能是 tracked 或 untracked"
            )
        if (
            not isinstance(self.state_revision, int)
            or isinstance(self.state_revision, bool)
            or self.state_revision < 0
        ):
            raise ValueError("ContextSourceControlState.state_revision 必须是非负整数")
        if self.updated_at is not None and (
            not isinstance(self.updated_at, str) or not self.updated_at
        ):
            raise ValueError(
                "ContextSourceControlState.updated_at 必须是非空字符串或 null"
            )
        if (
            self.latest_visible_committed_revision is not None
            and self.latest_revision is None
        ):
            raise ValueError(
                "ContextSourceControlState.latest_visible_committed_revision 不能脱离 "
                "latest_revision 存在"
            )

    def durable_fields(self) -> tuple[object, ...]:
        """返回决定是否需要写入的字段；不含 ``state_revision``/``updated_at``。"""
        return (
            self.owner.session_id,
            self.owner.thread_id,
            self.source_id,
            self.source_kind,
            self.name,
            self.binding_revision,
            self.tracking_status,
            self.latest_visible_committed_revision,
            self.latest_revision,
        )


@dataclass(frozen=True, slots=True)
class ContextSourceRebuildState:
    """同一 read snapshot 中的未消费 transition、控制状态与既有 source selection。"""

    transition: PendingPrefixEpochTransition
    states: tuple[ContextSourceControlState, ...]
    source_assembly: ContextAssemblySnapshot | None = None
    retained_source_refs: frozenset[tuple[str, str]] = frozenset()


ContextSourceControlOperation = Literal["register", "observe", "track", "untrack", "activate_skill"]


@dataclass(frozen=True, slots=True)
class ContextSourceControlMutation:
    """来源控制子端口的决策；state_revision 是本次写入的 CAS 基准。

    Skill 显式激活共同接受冻结版本与加载模式，applied 仍由 assembly seal 独占推进。
    owner 在一个事务内读取旧状态、验证决策并写入，CSM 不能提交 applied。
    """

    operation: ContextSourceControlOperation
    state: ContextSourceControlState
    # 旧 registration 的预期冻结值；state_revision 仍为冻结前 CAS 版本。
    replaced_state: ContextSourceControlState | None = None

    def __post_init__(self) -> None:
        if self.operation not in {"register", "observe", "track", "untrack", "activate_skill"}:
            raise ValueError("ContextSourceControlMutation.operation 非法")
        if not isinstance(self.state, ContextSourceControlState):
            raise TypeError("ContextSourceControlMutation.state 必须是 ContextSourceControlState")
        if self.operation in {"track", "untrack"} and self.state.tracking_status != (
            "tracked" if self.operation == "track" else "untracked"
        ):
            raise ValueError("ContextSourceControlMutation 与 tracking_status 不一致")
        if self.operation == "activate_skill" and (
            self.state.source_kind != "skill" or self.state.latest_revision is None
        ):
            raise ValueError("activate_skill 必须携带 Skill 的冻结内容版本")
        old = self.replaced_state
        if old is not None and (
            self.operation != "activate_skill"
            or not isinstance(old, ContextSourceControlState)
            or old.owner != self.state.owner
            or old.source_id == self.state.source_id
            or old.source_kind != "skill" or self.state.source_kind != "skill"
            or _normalize_skill_name(old.name) != _normalize_skill_name(self.state.name)
            or old.tracking_status != "untracked"
        ):
            raise ValueError("ContextSourceControlMutation rebind 必须冻结同 owner 的同名旧 Skill")


@dataclass(frozen=True, slots=True)
class ContextSourceControlReceipt:
    """同一控制事务确认的新状态和可选的已冻结旧 registration。"""

    state: ContextSourceControlState
    replaced_state: ContextSourceControlState | None = None


class ContextSourceControlStatePort(Protocol):
    """CSM 的注册/观察/跟踪写端口与只读 reconcile；事务归唯一 owner。"""

    def load_context_source_control_states(
        self,
        owner: ContextSourceOwnerKey,
    ) -> tuple[ContextSourceControlState, ...]:
        """读取该 owner 的全部控制状态；没有记录时返回空 tuple。"""
        ...

    def load_context_source_rebuild_state(
        self,
        owner: ContextSourceOwnerKey,
        *,
        checkpoint_ns: str = "",
    ) -> ContextSourceRebuildState | None:
        """只读已提交的 view rebuild；pending 不代表已应用的 prefix epoch。"""
        ...

    def apply_context_source_control_mutation(
        self,
        mutation: ContextSourceControlMutation,
    ) -> ContextSourceControlReceipt:
        """在同一 owner read/write 事务中消费控制决策，返回已确认的状态。

        控制字段与已存记录完全一致时必须幂等返回，不推进 ``state_revision``。
        """
        ...


__all__ = [
    "ContextSourceControlMutation",
    "ContextSourceControlOperation",
    "ContextSourceControlReceipt",
    "ContextSourceControlState",
    "ContextSourceControlStatePort",
    "ContextSourceOwnerKey",
    "ContextSourceTrackingStatus",
]
