"""Provider dispatch 前的 context plan seal 与短生命周期 handle。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, NotRequired, TypedDict, cast

from langchain_core.messages import BaseMessage

from app.agents.providers.wire_finalize import ChatWireProfile, ResponsesWireProfile
from app.core.model_request_context import SummaryCandidateScope
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.dispatch_ref import SealedContextDispatchRef
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.identity.item_projection import ItemProjectionIdentity
from app.domain.itemized.identity.tool_call_identity import (
    matches_tool_call_id,
    provider_tool_call_id,
)
from app.domain.itemized.records import parse_item_projection_manifest
from app.domain.itemized.request_plan import ContextRequestPlan
from app.domain.itemized.resource_activation import ResourceActivationSnapshotRef
from app.services.infrastructure.rollout_context.assembly.plans.registry import (
    read_registration,
)
from app.services.infrastructure.rollout_context.assembly.plans.types import (
    ContextPlanRegistration,
)
from app.services.infrastructure.rollout_context.checkpoint.seal.preparation import (
    prepare_registered_context,
    prepared_context_plan_id,
)
from app.services.infrastructure.rollout_context.runtime.context_sources.context_source_manager import (
    ContextSourceManager,
)

if TYPE_CHECKING:
    from _thread import RLock

    from app.services.infrastructure.rollout_context.store import RolloutContextStore


class ContextDispatchPayload(TypedDict):
    """Saver 交付的同一封存引用与精确投影，不包含框架运行时对象。"""

    snapshot: ContextAssemblySnapshot
    dispatch_ref: SealedContextDispatchRef
    assembly_id: str
    execution_id: str
    plan: ContextRequestPlan
    messages: list[BaseMessage]
    tools: list[dict[str, object]]
    losses: tuple[str, ...]
    native_projection: NotRequired[dict[str, object]]


class ContextDispatchOwnerMixin:
    """只负责 provider dispatch 的 plan seal、投影和 pending handle。"""

    if TYPE_CHECKING:
        # 这些端口由组合后的 Saver 提供；这里只声明 mixin 的实际依赖。
        _storage: RolloutContextStore
        _lock: RLock
        _prepared_dispatches: dict[tuple[str, str, str, str], list[dict[str, object]]]

        @staticmethod
        def context_owner_namespace(checkpoint_ns: str) -> str: ...

        def project_context_plan_for_dispatch(
            self, session_id: str, thread_id: str, plan: ContextRequestPlan, *,
            target_format: str, checkpoint_ns: str = "",
        ) -> tuple[list[BaseMessage], list[dict[str, object]], tuple[str, ...], dict[str, object] | None]: ...

    def issue_context_dispatch_ref(
        self, snapshot: ContextAssemblySnapshot, *, checkpoint_ns: str = "",
        attempt_key: str, callback_model_call_id: str,
    ) -> SealedContextDispatchRef:
        """用本次提交 receipt 签发引用；新 seal 不再冷读 assembly/selection。"""
        return SealedContextDispatchRef.from_snapshot(
            snapshot, checkpoint_ns=self.context_owner_namespace(checkpoint_ns),
            attempt_key=attempt_key, callback_model_call_id=callback_model_call_id,
        )

    def read_context_for_dispatch(self, reference: SealedContextDispatchRef) -> ContextDispatchPayload:
        """冷恢复只读精确 assembly；不访问 CSM、实时 ToolSet 或最新 view。"""
        if not isinstance(reference, SealedContextDispatchRef):
            raise TypeError("sealed-assembly binding violation: 缺少 typed dispatch reference")
        namespace = self.context_owner_namespace(reference.checkpoint_ns)
        if namespace != reference.checkpoint_ns:
            raise ValueError("sealed-assembly binding violation: namespace 不一致")
        with self._lock, self._storage.storage.catalog_body_validation_scope():
            snapshot = self._storage.assemblies.get_context_assembly(
                reference.session_id, reference.thread_id,
                assembly_id=reference.assembly_id, checkpoint_ns=namespace,
            )
            reference.validate_snapshot(snapshot)
            messages, tools, losses, native = self.project_context_plan_for_dispatch(
                reference.session_id, reference.thread_id, snapshot.as_sealed_plan(),
                target_format=reference.target_format, checkpoint_ns=namespace,
            )
        result: ContextDispatchPayload = {"snapshot": snapshot, "dispatch_ref": reference,
                  "assembly_id": snapshot.assembly_id, "plan": snapshot.as_sealed_plan(),
                  "execution_id": snapshot.execution_id,
                  "messages": messages, "tools": tools, "losses": losses}
        if native is not None:
            result["native_projection"] = native
        return result

    def load_tool_call_context_assembly(
        self, session_id: str, *, thread_id: str, tool_call_id: str,
        model_call_id: str | None = None, checkpoint_ns: str = "",
    ) -> ContextAssemblySnapshot:
        """按 canonical emitting assistant 定位工具原来的冻结请求，不读最新 view。"""
        checkpoint_ns = self.context_owner_namespace(checkpoint_ns)
        with self._storage.storage._connect(
            thread_id, checkpoint_ns, session_id=session_id, read_only=True,
        ) as connection:
            self._storage.storage._require_v2_runtime(connection)
            call_rows = connection.execute(
                "SELECT t.tool_call_id, i.projection_manifest_json "
                "FROM tool_calls t JOIN messages m "
                "ON m.message_sequence = t.assistant_message_sequence "
                "JOIN item_catalog i ON i.jsonl_offset = m.jsonl_offset "
                "AND i.jsonl_length = m.jsonl_length "
                "WHERE m.role = 'assistant' AND "
                "((? IS NULL AND t.tool_call_id = ?) OR "
                "json_extract(i.projection_manifest_json, "
                "'$.projection_identity.model_call_id') = ?)",
                (model_call_id, tool_call_id, model_call_id),
            ).fetchall()
            model_call_ids: set[str] = set()
            for stored_call_id, raw_manifest in call_rows:
                manifest = parse_item_projection_manifest(json.loads(raw_manifest))
                projection = manifest["projection_identity"]
                identity = ItemProjectionIdentity.from_dict(projection) if projection is not None else None
                if identity is None or identity.model_call_id is None:
                    if stored_call_id == tool_call_id:
                        raise RuntimeError(f"tool_call 缺少 typed model_call provenance: {tool_call_id}")
                    continue
                raw_call_id = provider_tool_call_id(identity, tool_call_id)
                if not raw_call_id or (model_call_id is None and raw_call_id == tool_call_id):
                    raise RuntimeError(
                        "tool_call 无 model-call scope 时必须提供与 canonical typed owner "
                        f"一致的非空 persisted scoped ID: {tool_call_id}"
                    )
                if matches_tool_call_id(identity, stored_call_id, tool_call_id):
                    model_call_ids.add(identity.model_call_id)
            if len(model_call_ids) != 1:
                raise RuntimeError(f"tool_call canonical assistant/model_call identity 缺失或歧义: {tool_call_id}")
            resolved_model_call_id = model_call_ids.pop()
            row = connection.execute(
                "SELECT assembly_id FROM model_calls WHERE session_id = ? "
                "AND thread_id = ? AND model_call_id = ?",
                (session_id, thread_id, resolved_model_call_id),
            ).fetchone()
            if row is None or not row[0]:
                raise RuntimeError(f"tool_call 原 model-call 缺少 sealed assembly: {tool_call_id}")
            assembly_id = row[0]
        assembly = self._storage.assemblies.get_context_assembly(
            session_id, thread_id, assembly_id=assembly_id, checkpoint_ns=checkpoint_ns,
        )
        if assembly.model_call_id != resolved_model_call_id:
            raise RuntimeError("tool_call 原 assembly model-call identity 不一致")
        return assembly

    def find_sealed_context_for_provider_creation(
        self, session_id: str, thread_id: str, *,
        plan_creation_idempotency_key: str, checkpoint_ns: str = "",
    ) -> ContextPlanRegistration | None:
        """按唯一 plan key 完整冷恢复；将核验结果交给本次派发继续消费。"""

        checkpoint_ns = self.context_owner_namespace(checkpoint_ns)
        plan_id = prepared_context_plan_id(plan_creation_idempotency_key)
        with self._lock, self._storage.storage.open_read_snapshot(
            session_id, thread_id, checkpoint_ns,
        ) as read_snapshot:
            connection = read_snapshot.connection
            row = connection.execute(
                "SELECT plan_state FROM context_plans WHERE session_id=? AND thread_id=? AND plan_id=?",
                (session_id, thread_id, plan_id),
            ).fetchone()
            if row is None:
                return None
            registered = read_registration(connection, session_id, thread_id, plan_id)
            if registered.plan_state != "sealed":
                return None
            if registered.assembly_id is None:
                raise ValueError("source-mismatch: sealed provider registration 缺少 assembly_id")
            snapshot, _ = self._storage.assemblies._read_context_assembly_connection(
                connection, session_id, thread_id, assembly_id=registered.assembly_id,
                checkpoint_ns=checkpoint_ns, registration=registered,
            )
        if (snapshot.session_id, snapshot.thread_id, snapshot.plan_id) != (
            session_id, thread_id, plan_id,
        ):
            raise ValueError("source-mismatch: sealed provider creation owner 不一致")
        return registered

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
        callback_model_call_id: str | None = None,
        activation_snapshot: ResourceActivationSnapshotRef,
        summary_candidate: SummaryCandidateScope | None = None,
        sealed_context: ContextPlanRegistration | None = None,
    ) -> ContextDispatchPayload:
        """最终过滤后显式注册；稳定请求 key 必须由 execution owner 提供。

        ``wire_params`` 是 execution 从实际模型绑定提取的纯数据有效参数
        （wire_finalize 提取器输出），Saver 只透传，不持有模型实例。
        """
        with self._storage.storage.catalog_body_validation_scope():
            prepared = prepare_registered_context(
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
                checkpoint_ns=self.context_owner_namespace(checkpoint_ns),
                summary_candidate=summary_candidate,
                sealed_context=sealed_context,
            )
            snapshot = prepared["snapshot"]
            if not isinstance(snapshot, ContextAssemblySnapshot):
                raise TypeError("sealed-assembly binding violation: Saver prepare 缺少 snapshot")
            reference = self.issue_context_dispatch_ref(
                snapshot, checkpoint_ns=checkpoint_ns,
                attempt_key=snapshot.plan_id,
                callback_model_call_id=callback_model_call_id if callback_model_call_id is not None else model_call_id,
            )
            prepared["dispatch_ref"] = reference
            with self._lock:
                key = (session_id, thread_id, self.context_owner_namespace(checkpoint_ns), execution_id)
                queue = self._prepared_dispatches.setdefault(key, [])
                existing = [item for item in queue if isinstance(stored := item.get("dispatch_ref"), SealedContextDispatchRef)
                            and stored.callback_model_call_id == reference.callback_model_call_id]
                if existing and existing[0]["dispatch_ref"] != reference:
                    raise ValueError("sealed-assembly binding violation: 同 callback 绑定了不同请求")
                if not existing:
                    queue.append(prepared)
            return cast(ContextDispatchPayload, prepared)

    def consume_prepared_context_for_dispatch(
        self,
        session_id: str,
        thread_id: str,
        *,
        execution_id: str,
        model_call_id: str,
        checkpoint_ns: str = "",
    ) -> ContextDispatchPayload | None:
        """把下一个已 sealed pre-dispatch plan 绑定到 model-call event。"""
        checkpoint_ns = self.context_owner_namespace(checkpoint_ns)
        with self._lock:
            key = (session_id, thread_id, checkpoint_ns, execution_id)
            pending = self._prepared_dispatches.get(key)
            if not pending:
                return None
            matches = [index for index, item in enumerate(pending)
                       if isinstance(reference := item.get("dispatch_ref"), SealedContextDispatchRef)
                       and reference.callback_model_call_id == model_call_id]
            if len(matches) != 1:
                raise RuntimeError("sealed-assembly binding violation: callback 没有唯一 prepared reference")
            result = pending.pop(matches[0])
            if not pending:
                del self._prepared_dispatches[key]
            return cast(ContextDispatchPayload, dict(result))

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
        checkpoint_ns = self.context_owner_namespace(checkpoint_ns)
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


__all__ = ["ContextDispatchOwnerMixin", "ContextDispatchPayload"]
