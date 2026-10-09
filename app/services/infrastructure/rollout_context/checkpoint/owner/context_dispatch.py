"""Provider dispatch 前的 context plan seal 与短生命周期 handle。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from langchain_core.messages import BaseMessage

from app.agents.providers.wire_finalize import ChatWireProfile, ResponsesWireProfile
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.resource_activation import ResourceActivationSnapshotRef
from app.services.infrastructure.rollout_context.checkpoint.seal.preparation import (
    prepare_registered_context,
    prepared_context_plan_id,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)


class ContextDispatchOwnerMixin:
    """只负责 provider dispatch 的 plan seal、投影和 pending handle。"""

    def find_sealed_context_for_provider_creation(
        self, session_id: str, thread_id: str, *,
        plan_creation_idempotency_key: str, checkpoint_ns: str = "",
    ) -> ContextAssemblySnapshot | None:
        """按唯一 plan key 恢复已封存事实；不读取 CSM/Registry 或新增准备 handle。"""

        checkpoint_ns = self._context_owner_namespace(checkpoint_ns)
        plan_id = prepared_context_plan_id(plan_creation_idempotency_key)
        with self._lock:
            registered = self._find_runtime_context_plan(
                session_id, thread_id, plan_id, checkpoint_ns,
            )
            if registered is None or registered.plan_state != "sealed":
                return None
            snapshot = self._storage.assemblies.get_context_assembly(
                session_id, thread_id, assembly_id=registered.assembly_id,
                checkpoint_ns=checkpoint_ns,
            )
        if (snapshot.session_id, snapshot.thread_id, snapshot.plan_id) != (
            session_id, thread_id, plan_id,
        ):
            raise ValueError("source-mismatch: sealed provider creation owner 不一致")
        return snapshot

    def prepare_context_for_provider(
        self,
        session_id: str,
        thread_id: str,
        *,
        turn_id: str | None,
        execution_id: str,
        plan_creation_idempotency_key: str,
        seal_idempotency_key: str,
        request_messages: Sequence[BaseMessage] = (),
        context_source_manager: ContextSourceManager,
        tool_set_supplier: Callable[[], ToolSetSnapshot],
        provider_version: str,
        wire_profile: ChatWireProfile | ResponsesWireProfile,
        wire_params: Mapping[str, object],
        target_format: str,
        checkpoint_ns: str = "",
        model_call_id: str,
        activation_snapshot: ResourceActivationSnapshotRef,
    ) -> dict[str, object]:
        """最终过滤后显式注册；稳定请求 key 必须由 middleware/runtime owner 提供。

        ``wire_params`` 是 Bridge 从实际模型/请求提取的纯数据有效参数
        （wire_finalize 提取器输出），Saver 只透传，不持有模型实例。
        """
        return prepare_registered_context(
            self,
            session_id,
            thread_id,
            turn_id=turn_id,
            execution_id=execution_id,
            plan_creation_idempotency_key=plan_creation_idempotency_key,
            seal_idempotency_key=seal_idempotency_key,
            request_messages=request_messages,
            context_source_manager=context_source_manager,
            tool_set_supplier=tool_set_supplier,
            provider_version=provider_version,
            wire_profile=wire_profile,
            wire_params=wire_params,
            target_format=target_format,
            model_call_id=model_call_id, activation_snapshot=activation_snapshot,
            checkpoint_ns=self._context_owner_namespace(checkpoint_ns),
        )

    def consume_prepared_context_for_dispatch(
        self,
        session_id: str,
        thread_id: str,
        *,
        execution_id: str,
        checkpoint_ns: str = "",
    ) -> dict[str, object] | None:
        """把下一个已 sealed pre-dispatch plan 绑定到 model-call event。"""
        checkpoint_ns = self._context_owner_namespace(checkpoint_ns)
        with self._lock:
            key = (session_id, thread_id, checkpoint_ns, execution_id)
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
        thread_id: str,
        *,
        execution_id: str,
        assembly_id: str,
        checkpoint_ns: str = "",
    ) -> None:
        """清除尚未被 model-start event 消费的短生命周期 pending handle。"""
        checkpoint_ns = self._context_owner_namespace(checkpoint_ns)
        with self._lock:
            key = (session_id, thread_id, checkpoint_ns, execution_id)
            pending = self._prepared_dispatches.get(key)
            if not pending:
                return
            pending[:] = [
                item for item in pending if item.get("assembly_id") != assembly_id
            ]
            if not pending:
                del self._prepared_dispatches[key]


__all__ = ["ContextDispatchOwnerMixin"]
