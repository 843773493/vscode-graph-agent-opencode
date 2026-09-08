"""Provider dispatch 前的 context plan seal 与短生命周期 handle。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from langchain_core.messages import BaseMessage

from app.domain.itemized.request_plan import (
    ContextContribution,
)
from app.services.infrastructure.rollout_context.checkpoint.seal.preparation import (
    prepare_registered_context,
)


class ContextDispatchOwnerMixin:
    """只负责 provider dispatch 的 plan seal、投影和 pending handle。"""

    def prepare_context_for_provider(
        self,
        session_id: str,
        *,
        turn_id: str,
        plan_creation_idempotency_key: str,
        seal_idempotency_key: str,
        request_messages: Sequence[BaseMessage] = (),
        prompt_contributions: Sequence[ContextContribution] = (),
        tool_snapshot: Sequence[Mapping[str, object]] = (),
        provider_version: str,
        target_format: str,
        checkpoint_ns: str = "",
    ) -> dict[str, object]:
        """最终过滤后显式注册；稳定请求 key 必须由 middleware/runtime owner 提供。"""
        return prepare_registered_context(
            self,
            session_id,
            turn_id=turn_id,
            plan_creation_idempotency_key=plan_creation_idempotency_key,
            seal_idempotency_key=seal_idempotency_key,
            request_messages=request_messages,
            prompt_contributions=prompt_contributions,
            tool_snapshot=tool_snapshot,
            provider_version=provider_version,
            target_format=target_format,
            checkpoint_ns=self._context_owner_namespace(checkpoint_ns),
        )

    def consume_prepared_context_for_dispatch(
        self,
        session_id: str,
        *,
        turn_id: str,
        checkpoint_ns: str = "",
    ) -> dict[str, object] | None:
        """把下一个已 sealed pre-dispatch plan 绑定到 model-call event。"""
        checkpoint_ns = self._context_owner_namespace(checkpoint_ns)
        with self._lock:
            key = (session_id, checkpoint_ns, turn_id)
            pending = self._prepared_dispatches.get(key)
            if not pending:
                return None
            result = pending.pop(0)
            if not pending:
                del self._prepared_dispatches[key]
            return dict(result)

    def discard_prepared_context_for_dispatch(
        self,
        session_id: str,
        *,
        turn_id: str,
        assembly_id: str,
        checkpoint_ns: str = "",
    ) -> None:
        """清除尚未被 model-start event 消费的短生命周期 pending handle。"""
        checkpoint_ns = self._context_owner_namespace(checkpoint_ns)
        with self._lock:
            key = (session_id, checkpoint_ns, turn_id)
            pending = self._prepared_dispatches.get(key)
            if not pending:
                return
            pending[:] = [
                item for item in pending if item.get("assembly_id") != assembly_id
            ]
            if not pending:
                del self._prepared_dispatches[key]


__all__ = ["ContextDispatchOwnerMixin"]
