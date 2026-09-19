"""SessionThread 唯一 mutation owner 的 typed intent 端口（B4 第一阶段）。

OpenSpec add-context-injection-lifecycle 2.3：一个 SessionThread 的所有
canonical append、source lifecycle decision、ToolSet switch 和 epoch rebuild
都必须表达为 domain 层定义的 ContextMutationIntent，由唯一
RolloutCheckpointSaver/ContextStore owner 消费。本模块只提供该端口的 typed
合同与 facade：owner 校验、幂等键去重和 failure outcome 显式化；不新建第二套
intent union，也不在这里实现持久化事务。

TODO(OpenSpec 2.3-B4)：saver owner 对该端口的 append/toolset/epoch 分支生产
实现与 CSM source 子端口的 typed decision 映射属 B4 后续切片；source pending
提交已由 ContextSourceManager.commit_model_call_pending 承载。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.domain.itemized.mutation_intents import (
    ContextMutationIntent,
    IntentFailureOutcome,
    MutationIntentOwner,
    intent_kind,
)


class SessionThreadMutationIntentPort(Protocol):
    """owner 侧的 intent 消费协议；实现由唯一 Saver/ContextStore owner 提供。

    实现必须在单个 owner transaction 内消费 intent；任何失败都通过异常
    显式暴露，并保证 intent.failure_outcome 描述的持久状态集合不变：
    不产生半 item、半 revision 或半 transition。
    """

    def consume_mutation_intent(self, intent: ContextMutationIntent) -> None:
        """在 owner 事务内消费一个 typed intent；失败必须抛出异常。"""
        ...


class MutationIntentPortError(RuntimeError):
    """mutation intent 端口合同错误的闭合基类；错误显式抛出，不静默降级。"""


class MutationIntentOwnerMismatch(MutationIntentPortError):
    """intent.owner 与端口 owner 不一致；端口未被触碰，零副作用。"""

    code = "mutation-intent-owner-mismatch"


class MutationIntentDuplicateConsumption(MutationIntentPortError):
    """同一幂等键的 intent 已被本 owner 消费；不重放端口调用。"""

    code = "mutation-intent-duplicate"

    def __init__(self, message: str, *, idempotency_key: str) -> None:
        super().__init__(message)
        self.idempotency_key = idempotency_key


class MutationIntentConsumptionRejected(MutationIntentPortError):
    """owner 事务消费失败；按 intent.failure_outcome 显式暴露失败语义。

    幂等键不记录为已消费：失败重试按原 identity 复用，不产生半状态。
    """

    code = "mutation-intent-rejected"

    def __init__(
        self,
        message: str,
        *,
        idempotency_key: str,
        failure_outcome: IntentFailureOutcome,
    ) -> None:
        super().__init__(message)
        self.idempotency_key = idempotency_key
        self.failure_outcome = failure_outcome


@dataclass(frozen=True, slots=True)
class MutationIntentConsumptionReceipt:
    """一次成功消费的纯值回执：只携带 domain 合同字段，不伪造状态。"""

    intent_kind: str
    idempotency_key: str
    failure_outcome: IntentFailureOutcome


class SessionThreadMutationIntents:
    """唯一 SessionThread owner 的 mutation intent 消费 facade。

    - owner 必须与 intent.owner 精确相等；错误 owner 直接拒绝且不触碰端口。
    - 同一幂等键在本 facade 生命周期内只消费一次；重复提交显式失败，
      不重放端口调用，也不覆盖既有事实。
    - 端口消费失败时按 intent.failure_outcome 显式抛错；幂等键不记录，
      重试按原 identity 复用，不产生半 item/半 revision/半 transition。
    """

    def __init__(
        self,
        *,
        owner: MutationIntentOwner,
        port: SessionThreadMutationIntentPort,
    ) -> None:
        if not isinstance(owner, MutationIntentOwner):
            raise TypeError(
                "SessionThreadMutationIntents.owner 需要 MutationIntentOwner"
            )
        self._owner = owner
        self._port = port
        self._consumed_keys: set[str] = set()

    @property
    def owner(self) -> MutationIntentOwner:
        return self._owner

    @property
    def consumed_keys(self) -> tuple[str, ...]:
        """已成功消费的幂等键；仅供合同测试与诊断，不参与业务判断。"""
        return tuple(sorted(self._consumed_keys))

    def consume(
        self,
        intent: ContextMutationIntent,
    ) -> MutationIntentConsumptionReceipt:
        """校验 owner/幂等后把 intent 交给唯一 owner 端口消费。"""
        kind = intent_kind(intent)
        if intent.owner != self._owner:
            raise MutationIntentOwnerMismatch(
                "mutation-intent-owner-mismatch: intent owner 与端口 owner 不一致: "
                f"expected=({self._owner.session_id},{self._owner.thread_id}) "
                f"actual=({intent.owner.session_id},{intent.owner.thread_id})"
            )
        key = intent.idempotency_key
        if key in self._consumed_keys:
            raise MutationIntentDuplicateConsumption(
                "mutation-intent-duplicate: 同一幂等键的 intent 已被本 owner 消费: "
                f"kind={kind}, idempotency_key={key}",
                idempotency_key=key,
            )
        try:
            self._port.consume_mutation_intent(intent)
        except MutationIntentPortError:
            raise
        except Exception as error:
            raise MutationIntentConsumptionRejected(
                "mutation-intent-rejected: owner 事务失败，intent 未推进: "
                f"kind={kind}, idempotency_key={key}, "
                f"failure_outcome={intent.failure_outcome}, error={error}",
                idempotency_key=key,
                failure_outcome=intent.failure_outcome,
            ) from error
        self._consumed_keys.add(key)
        return MutationIntentConsumptionReceipt(
            intent_kind=kind,
            idempotency_key=key,
            failure_outcome=intent.failure_outcome,
        )


__all__ = [
    "MutationIntentConsumptionReceipt",
    "MutationIntentConsumptionRejected",
    "MutationIntentDuplicateConsumption",
    "MutationIntentOwnerMismatch",
    "MutationIntentPortError",
    "SessionThreadMutationIntentPort",
    "SessionThreadMutationIntents",
]
