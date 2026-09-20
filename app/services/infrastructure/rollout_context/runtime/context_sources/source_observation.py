"""CSM 的 typed SourceObservation / lifecycle decision / control state 扩展。

OpenSpec add-context-injection-lifecycle 1.5：producer 与 CSM 之间影响
tracking/激活/选择/替换的核心字段必须走本模块的 typed 值对象；自由
metadata/extensions 中的控制 flag、ordinal、tracking/role 不再拥有任何
解释权。extensions 只能原样透传或审计，不参与 tracking、diff 基准、
pending/applied 推进与 dispatch 决策。

本模块是合同层：字段校验与幂等键派生在这里闭合；把 observation 变成
owner transaction 的持久化由唯一 ContextStore owner 的 intent 端口
（OpenSpec 2.3/B4）完成，本模块不执行 I/O。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from app.domain.itemized.mutation_intents import (
    ApplySourceLifecycleDecision,
    MutationIntentOwner,
    SourceActivationBoundary,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_control_state import (
    ContextSourceOwnerKey,
)

SourceTrackingMode = Literal["snapshot", "tracked", "untrack"]
"""producer 声明的 tracking 模式；与 skill_load 的 mode 闭合集一致。"""


def _require_non_empty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} 必须是非空字符串")
    return value


@dataclass(frozen=True, slots=True)
class SourceObservation:
    """producer 提交给 CSM 的一次结构化来源观察。

    字段即决策入口：owner thread、source/facet identity、published
    semantic revision、activation boundary、tracking mode、base/delta
    关系与幂等键全部显式声明。content 是受信 source owner 已验证的正文
    快照；None 表示只登记「该来源有新事实」，正文由 owner 的权威内存
    快照在消费点提供。extensions 必须是 namespaced/versioned 的透传
    envelope，CSM 不读取其中的任何键。
    """

    owner: ContextSourceOwnerKey
    source_id: str
    source_kind: str
    name: str
    revision: str
    tracking_mode: SourceTrackingMode
    activation_boundary: SourceActivationBoundary = "turn"
    from_revision: str | None = None
    content: str | None = None
    extensions: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.owner, ContextSourceOwnerKey):
            raise TypeError("SourceObservation.owner 必须是 ContextSourceOwnerKey")
        for field_name in ("source_id", "source_kind", "name", "revision"):
            _require_non_empty(
                getattr(self, field_name), f"SourceObservation.{field_name}"
            )
        if self.tracking_mode not in {"snapshot", "tracked", "untrack"}:
            raise ValueError(
                f"SourceObservation.tracking_mode 非法: {self.tracking_mode!r}"
            )
        if self.activation_boundary not in {"turn", "model_call"}:
            raise ValueError(
                f"SourceObservation.activation_boundary 非法: "
                f"{self.activation_boundary!r}"
            )
        if self.content is not None and not isinstance(self.content, str):
            raise TypeError("SourceObservation.content 必须是字符串或 None")
        if self.from_revision is not None:
            _require_non_empty(
                self.from_revision, "SourceObservation.from_revision"
            )
            if self.from_revision == self.revision:
                raise ValueError(
                    "SourceObservation: from_revision 不得等于当前 revision"
                )
        if not isinstance(self.extensions, Mapping):
            raise TypeError("SourceObservation.extensions 必须是 Mapping")

    @property
    def observation_id(self) -> str:
        """稳定幂等键：同一来源同一 revision 的重复观察按 identity 复用。"""
        return (
            f"source-observation:{self.owner.session_id}:{self.owner.thread_id}:"
            f"{self.source_id}:{self.revision}"
        )


@dataclass(frozen=True, slots=True)
class ContextSourceControlStateExtension:
    """与既有 ContextSourceControlState 配对的 typed 扩展事实。

    携带旧控制状态没有覆盖的决策字段：activation boundary、base/delta
    关系、desired/applied revision 与产生本状态的决策幂等键。它仍是纯
    值对象；持久化列与端口扩展由 OpenSpec 2.3/B4 落地，恢复时缺失核心
    字段必须显式失败，不得用当前文件/事件/扩展补齐。
    """

    owner: ContextSourceOwnerKey
    source_id: str
    activation_boundary: SourceActivationBoundary
    base_revision: str | None
    desired_revision: str | None
    latest_visible_committed_revision: str | None
    decision_idempotency_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.owner, ContextSourceOwnerKey):
            raise TypeError(
                "ContextSourceControlStateExtension.owner 必须是 "
                "ContextSourceOwnerKey"
            )
        _require_non_empty(
            self.source_id, "ContextSourceControlStateExtension.source_id"
        )
        if self.activation_boundary not in {"turn", "model_call"}:
            raise ValueError(
                "ContextSourceControlStateExtension.activation_boundary 非法: "
                f"{self.activation_boundary!r}"
            )
        for field_name in (
            "base_revision",
            "desired_revision",
            "latest_visible_committed_revision",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _require_non_empty(
                    value,
                    f"ContextSourceControlStateExtension.{field_name}",
                )
        _require_non_empty(
            self.decision_idempotency_key,
            "ContextSourceControlStateExtension.decision_idempotency_key",
        )


def build_source_lifecycle_decision(
    observation: SourceObservation,
    *,
    decision_kind: Literal[
        "base",
        "delta",
        "rebuild",
        "track",
        "untrack",
        "observe_pending",
    ],
    pending_only: bool = False,
) -> ApplySourceLifecycleDecision:
    """把 typed observation 确定性映射为 owner 可消费的 intent 分支。

    映射只使用 observation 的 typed 字段；extensions 不参与任何分支或
    字段取值。tracking mode 到 control 字段的对应关系：snapshot/
    observe_pending 不改变 tracking status，tracked 声明 tracked，
    untrack 声明 frozen（untracked）。
    """
    if not isinstance(observation, SourceObservation):
        raise TypeError("build_source_lifecycle_decision 需要 SourceObservation")
    if decision_kind in {"base", "delta", "rebuild"} and observation.revision is None:
        raise ValueError("base/delta/rebuild 决策要求 observation 携带 revision")
    tracking_status: Literal["tracked", "untracked"] | None = None
    if observation.tracking_mode == "tracked":
        tracking_status = "tracked"
    elif observation.tracking_mode == "untrack":
        tracking_status = "untracked"
    revision: str | None = observation.revision
    if decision_kind == "untrack":
        # untrack 只声明冻结；不携带新 revision 事实。
        revision = None
    content = observation.content
    item_id: str | None = None
    if content is not None:
        revision_token = revision or "pending"
        item_id = (
            "item-context-source:"
            f"{observation.source_id}:{revision_token}:{decision_kind}"
        )
    return ApplySourceLifecycleDecision(
        owner=MutationIntentOwner(
            session_id=observation.owner.session_id,
            thread_id=observation.owner.thread_id,
        ),
        source_id=observation.source_id,
        source_kind=observation.source_kind,
        name=observation.name,
        decision_kind=decision_kind,
        revision=revision,
        from_revision=observation.from_revision if decision_kind == "delta" else None,
        activation_boundary=observation.activation_boundary,
        tracking_status=tracking_status,
        pending_only=pending_only,
        content=content,
        item_id=item_id,
    )


__all__ = [
    "ContextSourceControlStateExtension",
    "SourceObservation",
    "SourceTrackingMode",
    "build_source_lifecycle_decision",
]
