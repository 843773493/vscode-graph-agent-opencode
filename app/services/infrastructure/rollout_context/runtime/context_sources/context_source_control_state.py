"""CSM 控制状态的 typed 结构与只读/写端口合同。

本模块只定义值对象和 ``Protocol``，不执行任何 I/O。``ContextSourceManager``
只通过该端口读写自己的控制状态；SQLite 表、owner 锁和事务边界由唯一的
``RolloutCheckpointSaver``/ContextStore owner 实现。

控制状态按精确 ``(session_id, thread_id)`` 归属：main thread 使用
``MAIN_THREAD_ID``，durable child thread 必须使用真实 thread ID。它只保存
稳定 source identity、catalog/来源绑定 revision、observed/applied revision
和 untrack（frozen）状态；locator、handle 和正文归 Registry/实际 source
owner 所有，并在重启后由它们重建，不进入本状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

MAIN_THREAD_ID = "main"
"""Session 级产品入口的 main thread 身份。

裸 ``session_id`` 等价于 main thread，但控制状态必须使用稳定的
``MAIN_THREAD_ID`` 而不是 session_id，避免同一 thread 出现两个持久化 key。
"""

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
                raise ValueError(
                    f"ContextSourceOwnerKey.{field_name} 必须是非空字符串"
                )


@dataclass(frozen=True, slots=True)
class ContextSourceControlState:
    """一个 tracked registration 的持久化控制状态。

    ``state_revision`` 是该 registration 在 owner 侧的单调持久化版本：每次
    真正改变控制字段的写入都会 +1，用于乐观冲突检测，并为将来的
    checkpoint-versioned 恢复保留稳定的状态锚点。
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
                    "ContextSourceControlState."
                    f"{field_name} 必须是非空字符串或 null"
                )
        if self.tracking_status not in {"tracked", "untracked"}:
            raise ValueError(
                "ContextSourceControlState.tracking_status 只能是 "
                "tracked 或 untracked"
            )
        if (
            not isinstance(self.state_revision, int)
            or isinstance(self.state_revision, bool)
            or self.state_revision < 0
        ):
            raise ValueError(
                "ContextSourceControlState.state_revision 必须是非负整数"
            )
        if self.updated_at is not None and (
            not isinstance(self.updated_at, str) or not self.updated_at
        ):
            raise ValueError(
                "ContextSourceControlState.updated_at 必须是非空字符串或 null"
            )
        if self.latest_visible_committed_revision is not None and self.latest_revision is None:
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


class ContextSourceControlStatePort(Protocol):
    """CSM 控制状态在唯一 owner 上的读写端口；实现方拥有事务与锁。

    TODO: OpenSpec 2.3 要求 owner 暴露 register/observe/track/untrack/reconcile
    四类 mutation intent 子端口，并让所有分支共享同一 read snapshot 与 owner
    事务。当前端口只提供“按 owner 读取全部状态”和“原子 upsert 单条状态”两个
    最小能力，mutation intent 的细分由 CSM 在内存中判定后调用这里。
    """

    def load_context_source_control_states(
        self,
        owner: ContextSourceOwnerKey,
    ) -> tuple[ContextSourceControlState, ...]:
        """读取该 owner 的全部控制状态；没有记录时返回空 tuple。"""
        ...

    def save_context_source_control_state(
        self,
        state: ContextSourceControlState,
    ) -> ContextSourceControlState:
        """在单个 owner 事务中 upsert 一条状态，返回 owner 确认后的值。

        控制字段与已存记录完全一致时必须幂等返回，不推进 ``state_revision``。
        """
        ...


__all__ = [
    "MAIN_THREAD_ID",
    "ContextSourceControlState",
    "ContextSourceControlStatePort",
    "ContextSourceOwnerKey",
    "ContextSourceTrackingStatus",
]