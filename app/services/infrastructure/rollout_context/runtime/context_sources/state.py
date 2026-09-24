"""Context source registration 的运行时状态。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceControlState,
    ContextSourceTrackingStatus,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.models import (
    ContextSourceDescriptor,
)


@dataclass(slots=True)
class SourceState:
    """一个已登记来源的运行时状态。

    ``descriptor`` 为 ``None`` 表示该 registration 由持久化控制状态恢复，
    但 source owner 还没有用当前 catalog descriptor 重新绑定 locator；此时
    identity/revision 仍然有效，只是不能解析内部 locator。
    """

    source_id: str
    source_kind: str
    name: str
    descriptor: ContextSourceDescriptor | None = None
    description: str | None = None
    binding_revision: str | None = None
    tracking_status: ContextSourceTrackingStatus = "untracked"
    latest_visible_committed_revision: str | None = None
    latest_visible_committed_content: str | None = None
    latest_revision: str | None = None
    latest_content: str | None = None
    observed_revisions: list[str] = field(default_factory=list)
    pending_kind: Literal["activation", "delta", "rebuild"] | None = None
    persisted_state_revision: int = 0
    persisted_fields: tuple[object, ...] | None = None

    @property
    def tracked(self) -> bool:
        return self.tracking_status == "tracked"

    def bind_descriptor(self, descriptor: ContextSourceDescriptor) -> None:
        """把 Registry 重建的 descriptor 绑定回已恢复的 registration。"""
        if self.source_kind != descriptor.source_kind or self.name != descriptor.name:
            raise ValueError(
                "ContextSourceManager source identity 冲突: "
                f"source_id={descriptor.source_id} "
                f"registered_kind={self.source_kind} requested_kind={descriptor.source_kind} "
                f"registered_name={self.name} requested_name={descriptor.name}"
            )
        current = self.descriptor
        if current is not None and current != descriptor:
            raise ValueError(
                "ContextSourceManager source descriptor 冲突: "
                f"source_id={descriptor.source_id}"
            )
        self.descriptor = descriptor
        self.description = descriptor.description

    def has_durable_interest(self) -> bool:
        """从未激活/跟踪的目录注册不是需要跨重启保留的控制状态。"""
        return (
            self.tracking_status == "tracked"
            or self.latest_visible_committed_revision is not None
            or self.latest_revision is not None
            or self.persisted_state_revision > 0
        )

    @classmethod
    def from_control_state(cls, stored: ContextSourceControlState) -> SourceState:
        """从持久化控制状态恢复 registration。

        tracked 且 latest_revision 为空是合法状态：registration 已登记但首帧
        观察尚未提交（注册后、首帧前重启）；恢复后由下一次 observation 产生
        activation 首帧，无需回填。
        """
        return cls(
            source_id=stored.source_id,
            source_kind=stored.source_kind,
            name=stored.name,
            binding_revision=stored.binding_revision,
            tracking_status=stored.tracking_status,
            latest_visible_committed_revision=stored.latest_visible_committed_revision,
            latest_revision=stored.latest_revision,
            persisted_state_revision=stored.state_revision,
            persisted_fields=stored.durable_fields(),
        )

__all__ = ["SourceState"]
