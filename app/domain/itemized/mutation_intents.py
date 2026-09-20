"""统一 ContextMutationIntent 合同：四分支 mutation 的唯一领域入口。

OpenSpec add-context-injection-lifecycle 1.1：一个 SessionThread 的所有
影响 canonical history、active view、source control state、ToolSet binding
和 sealed assembly 的变更都必须表达为语义互斥的 mutation intent，由唯一的
RolloutCheckpointSaver/ContextStore owner 在同一 read snapshot 与 owner
transaction 中消费。本模块只定义纯值对象合同；事务边界、锁和持久化由
infrastructure 侧 owner 实现，本模块不得反向导入它们。

四分支的 domain owner：
- AppendCanonicalItemIntent —— Turn/model stream/tool execution 等
  canonical 事实 producer；
- ApplySourceLifecycleDecision —— ContextSourceManager（CSM）；
- SwitchToolSetIntent —— ToolSelectionStore/ToolService 控制面；
- RebuildContextEpochIntent —— rewind/compaction rebuild owner。

每支都携带稳定的幂等键（由稳定字段确定性派生）与明确的 failure outcome
（失败时保证不推进的持久状态集合）；owner 消费失败必须保持该 outcome，
不得产生半 item、半 revision 或半 transition。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

IntentFailureOutcome = Literal[
    "reject_atomic",
    "keep_pending",
    "keep_applied_toolset",
    "keep_old_view",
]
"""intent 失败时的持久状态保证（failure outcome 闭合集）。

- reject_atomic：整个 owner transaction 拒绝，不产生任何新 item。
- keep_pending：source 保持 pending/applied 不变，不推进 diff 基准。
- keep_applied_toolset：applied ToolSet revision 与 prefix epoch 不变。
- keep_old_view：旧 active view 不变；已提交的 pending transition 保留
  且不可 dispatch，等待重试。
"""


CanonicalItemKind = Literal[
    "user_message",
    "attachment",
    "assistant_message",
    "reasoning",
    "tool_call",
    "tool_result",
]
"""canonical item 的闭合协议种类；CSM 不接管其生命周期。"""


SourceDecisionKind = Literal[
    "base",
    "delta",
    "rebuild",
    "track",
    "untrack",
    "observe_pending",
]
"""CSM source lifecycle 决策的闭合集。"""


SourceActivationBoundary = Literal["turn", "model_call"]
"""source 激活边界：默认 Turn 快照或显式 model_call 快照。"""


SourceItemTurnScope = Literal["ambient", "pending_next_turn"]
"""source item 的非 Turn 作用域；source 不得伪装成真实用户 Turn。"""


EpochRebuildReason = Literal["compaction", "rewind"]
"""可提交 RebuildContextEpochIntent 的重建原因。

首次 assembly 由 owner 直接初始化 epoch；ToolSet hard rebase 属
SwitchToolSetIntent 的安全边界语义，不使用本 intent。
"""


def _require_non_empty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} 必须是非空字符串")
    return value


def _require_non_negative_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{field} 必须是非负整数")
    return value


@dataclass(frozen=True, slots=True)
class MutationIntentOwner:
    """mutation intent 的唯一 owner：一个精确的 (session_id, thread_id)。

    Session 只保存 main-thread 路由与 thread catalog，不拥有 canonical
    context；durable child thread 必须使用真实 thread ID。
    """

    session_id: str
    thread_id: str

    def __post_init__(self) -> None:
        _require_non_empty(self.session_id, "MutationIntentOwner.session_id")
        _require_non_empty(self.thread_id, "MutationIntentOwner.thread_id")


@dataclass(frozen=True, slots=True)
class AppendCanonicalItemIntent:
    """一次性 canonical 事实追加：真实 user input/attachment、assistant/
    reasoning、tool call/result。

    不变量：item identity 全局稳定；tool_call/tool_result 必须携带配对的
    tool_call_id，其余种类禁止携带；不建立 source revision 或 tracking
    registration。幂等键由 item_id 派生，重复提交同一事实按 identity 复用
    而非覆盖。
    """

    owner: MutationIntentOwner
    item_id: str
    item_kind: CanonicalItemKind
    origin_turn_id: str
    tool_call_id: str | None = None
    append_ordinal: int | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.item_id, "AppendCanonicalItemIntent.item_id")
        _require_non_empty(
            self.origin_turn_id, "AppendCanonicalItemIntent.origin_turn_id"
        )
        if self.item_kind not in {
            "user_message",
            "attachment",
            "assistant_message",
            "reasoning",
            "tool_call",
            "tool_result",
        }:
            raise ValueError(
                f"AppendCanonicalItemIntent.item_kind 非法: {self.item_kind!r}"
            )
        if self.item_kind in {"tool_call", "tool_result"}:
            _require_non_empty(
                self.tool_call_id, "AppendCanonicalItemIntent.tool_call_id"
            )
        elif self.tool_call_id is not None:
            raise ValueError(
                "AppendCanonicalItemIntent: 只有 tool_call/tool_result 允许携带 "
                "tool_call_id"
            )
        if self.append_ordinal is not None:
            _require_non_negative_int(
                self.append_ordinal, "AppendCanonicalItemIntent.append_ordinal"
            )

    @property
    def idempotency_key(self) -> str:
        return (
            f"append:{self.owner.session_id}:{self.owner.thread_id}:{self.item_id}"
        )

    @property
    def failure_outcome(self) -> IntentFailureOutcome:
        return "reject_atomic"


@dataclass(frozen=True, slots=True)
class ApplySourceLifecycleDecision:
    """CSM 对 instruction/file/runtime source 的 base/delta/tracking/恢复决策。

    不变量：base/rebuild 不得携带 from_revision；delta 必须携带与 revision
    不同的 from_revision；untrack 只声明冻结，不携带新 revision 事实；
    pending_only=True 表示异步 producer 在没有 model call 时到达，owner 只
    提交幂等 pending observation/ambient item，不推进 applied/diff 基准。
    """

    owner: MutationIntentOwner
    source_id: str
    source_kind: str
    name: str
    decision_kind: SourceDecisionKind
    revision: str | None
    from_revision: str | None = None
    activation_boundary: SourceActivationBoundary = "turn"
    tracking_status: Literal["tracked", "untracked"] | None = None
    pending_only: bool = False
    # source 正文是 intent 的 typed 载体。它不进入 extensions，也不允许 owner
    # 从当前文件/事件重新读取；None 仅用于 track/untrack 等没有 item 的决策。
    content: str | None = None
    item_id: str | None = None
    turn_scope: SourceItemTurnScope = "pending_next_turn"
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_non_empty(self.source_id, "ApplySourceLifecycleDecision.source_id")
        _require_non_empty(
            self.source_kind, "ApplySourceLifecycleDecision.source_kind"
        )
        _require_non_empty(self.name, "ApplySourceLifecycleDecision.name")
        if self.decision_kind not in {
            "base",
            "delta",
            "rebuild",
            "track",
            "untrack",
            "observe_pending",
        }:
            raise ValueError(
                f"ApplySourceLifecycleDecision.decision_kind 非法: "
                f"{self.decision_kind!r}"
            )
        if self.revision is not None:
            _require_non_empty(
                self.revision, "ApplySourceLifecycleDecision.revision"
            )
        if self.decision_kind in {"base", "delta", "rebuild"} and (
            self.revision is None
        ):
            raise ValueError(
                "ApplySourceLifecycleDecision: base/delta/rebuild 必须携带 "
                "published semantic revision"
            )
        if self.decision_kind == "delta":
            _require_non_empty(
                self.from_revision, "ApplySourceLifecycleDecision.from_revision"
            )
            if self.from_revision == self.revision:
                raise ValueError(
                    "ApplySourceLifecycleDecision: delta 的 from_revision 不得等于 "
                    "目标 revision"
                )
        elif self.from_revision is not None:
            raise ValueError(
                "ApplySourceLifecycleDecision: 只有 delta 允许携带 from_revision"
            )
        if self.decision_kind == "untrack":
            if self.revision is not None:
                raise ValueError(
                    "ApplySourceLifecycleDecision: untrack 不得携带新 revision"
                )
            if self.tracking_status not in (None, "untracked"):
                raise ValueError(
                    "ApplySourceLifecycleDecision: untrack 的 tracking_status "
                    "只能是 untracked 或 None"
                )
        if self.tracking_status is not None and self.tracking_status not in {
            "tracked",
            "untracked",
        }:
            raise ValueError(
                "ApplySourceLifecycleDecision.tracking_status 只能是 tracked/"
                "untracked/None"
            )
        if self.content is not None and not isinstance(self.content, str):
            raise TypeError("ApplySourceLifecycleDecision.content 必须是字符串或 None")
        if self.item_id is not None and (
            not isinstance(self.item_id, str) or not self.item_id
        ):
            raise ValueError(
                "ApplySourceLifecycleDecision.item_id 必须是非空字符串或 None"
            )
        if self.turn_scope not in {"ambient", "pending_next_turn"}:
            raise ValueError(
                "ApplySourceLifecycleDecision.turn_scope 只能是 ambient/"
                f"pending_next_turn: {self.turn_scope!r}"
            )
        if not isinstance(self.metadata, Mapping):
            raise TypeError("ApplySourceLifecycleDecision.metadata 必须是 object")
        if self.decision_kind in {"base", "delta", "rebuild", "observe_pending"}:
            if self.content is None:
                raise ValueError(
                    "ApplySourceLifecycleDecision 的 source item 决策必须携带 content"
                )
            if self.item_id is None:
                raise ValueError(
                    "ApplySourceLifecycleDecision 的 source item 决策必须携带 item_id"
                )
        elif self.content is not None or self.item_id is not None:
            raise ValueError(
                "ApplySourceLifecycleDecision 的 track/untrack 不得携带 source item"
            )

    @property
    def idempotency_key(self) -> str:
        revision = self.revision if self.revision is not None else "any"
        return (
            f"source-lifecycle:{self.owner.session_id}:{self.owner.thread_id}:"
            f"{self.source_id}:{self.decision_kind}:{revision}"
        )

    @property
    def failure_outcome(self) -> IntentFailureOutcome:
        return "keep_pending"


@dataclass(frozen=True, slots=True)
class SwitchToolSetIntent:
    """应用模型可见工具与 policy 的新 desired revision 并触发 hard rebase。

    不变量：desired revision 与 snapshot identity 非空；outstanding tool
    call 未收敛时 owner 不得 seal 使用新 ToolSet 的请求；同 Turn 允许多个
    epoch，但 applied 历史不可覆盖。
    """

    owner: MutationIntentOwner
    desired_revision: str
    tool_set_snapshot_id: str
    tool_policy_version: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty(
            self.desired_revision, "SwitchToolSetIntent.desired_revision"
        )
        _require_non_empty(
            self.tool_set_snapshot_id, "SwitchToolSetIntent.tool_set_snapshot_id"
        )
        if self.tool_policy_version is not None:
            _require_non_empty(
                self.tool_policy_version,
                "SwitchToolSetIntent.tool_policy_version",
            )

    @property
    def idempotency_key(self) -> str:
        return (
            f"toolset-switch:{self.owner.session_id}:{self.owner.thread_id}:"
            f"{self.desired_revision}"
        )

    @property
    def failure_outcome(self) -> IntentFailureOutcome:
        return "keep_applied_toolset"


@dataclass(frozen=True, slots=True)
class RebuildContextEpochIntent:
    """compaction/rewind 提交 active view、版本化 CSM control state 和
    PendingPrefixEpochTransition。

    不变量：view/control revision 必须是非负单调版本；pending transition
    没有 wire bytes 且不是 applied epoch，只可由下一次 model-call
    preparation 与首个新 epoch assembly seal 原子消费；intent 失败保持旧
    view，成功提交的 view/transition 不因后续 source 读取失败被回滚。
    """

    owner: MutationIntentOwner
    epoch_reason: EpochRebuildReason
    view_revision: int
    control_revision: int

    def __post_init__(self) -> None:
        if self.epoch_reason not in {"compaction", "rewind"}:
            raise ValueError(
                f"RebuildContextEpochIntent.epoch_reason 非法: "
                f"{self.epoch_reason!r}"
            )
        _require_non_negative_int(
            self.view_revision, "RebuildContextEpochIntent.view_revision"
        )
        _require_non_negative_int(
            self.control_revision, "RebuildContextEpochIntent.control_revision"
        )

    @property
    def idempotency_key(self) -> str:
        return (
            f"epoch-rebuild:{self.owner.session_id}:{self.owner.thread_id}:"
            f"{self.epoch_reason}:{self.view_revision}:{self.control_revision}"
        )

    @property
    def failure_outcome(self) -> IntentFailureOutcome:
        return "keep_old_view"


ContextMutationIntent = (
    AppendCanonicalItemIntent
    | ApplySourceLifecycleDecision
    | SwitchToolSetIntent
    | RebuildContextEpochIntent
)
"""语义互斥的 mutation intent union；owner 按 discriminator 分派消费。"""


def intent_kind(intent: ContextMutationIntent) -> str:
    """返回 intent 的稳定 discriminator，供 owner 消费入口分派。"""
    if isinstance(intent, AppendCanonicalItemIntent):
        return "append_canonical_item"
    if isinstance(intent, ApplySourceLifecycleDecision):
        return "apply_source_lifecycle_decision"
    if isinstance(intent, SwitchToolSetIntent):
        return "switch_tool_set"
    if isinstance(intent, RebuildContextEpochIntent):
        return "rebuild_context_epoch"
    raise TypeError(f"未知 ContextMutationIntent 分支: {type(intent).__name__}")


__all__ = [
    "AppendCanonicalItemIntent",
    "ApplySourceLifecycleDecision",
    "CanonicalItemKind",
    "ContextMutationIntent",
    "EpochRebuildReason",
    "IntentFailureOutcome",
    "MutationIntentOwner",
    "RebuildContextEpochIntent",
    "SourceActivationBoundary",
    "SourceDecisionKind",
    "SourceItemTurnScope",
    "SwitchToolSetIntent",
    "intent_kind",
]
