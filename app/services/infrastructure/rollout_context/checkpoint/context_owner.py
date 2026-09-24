"""RolloutCheckpointSaver 的 context owner 与 provider projection 边界。

本 mixin 只封装 Saver 对已提交 context plan/assembly 的 ownership、detail
和 projector 调用；checkpoint/fork persistence 仍由 Saver/persistence owner
提供，避免业务层旁路 RolloutStorage。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace

from langchain_core.messages import BaseMessage

from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.records import CanonicalItemRecord
from app.domain.itemized.refs import ContextRef, ToolSetRef
from app.domain.itemized.request_plan import ContextRequestPlan
from app.services.infrastructure.rollout_context.checkpoint.composition import (
    ContextPlanCompositionMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.context_details import (
    ContextDetailOwnerMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.context_dispatch import (
    ContextDispatchOwnerMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.context_projection import (
    ContextProjectionOwnerMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.resource_activation import (
    ResourceActivationOwnerMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.seal.retry import (
    require_key,
    runtime_draft,
)
from app.services.infrastructure.rollout_context.provider.toolset_request_bridge import (
    project_tool_set_ref,
)
from app.services.mapping.itemized.history import project_history_plan
from app.services.mapping.itemized.langchain import project_context_plan
from app.services.mapping.itemized.projection import (
    ProjectionEvidence,
    build_projection_evidence,
)


class ContextOwnerMixin(
    ContextDispatchOwnerMixin,
    ContextDetailOwnerMixin,
    ContextPlanCompositionMixin,
    ContextProjectionOwnerMixin,
    ResourceActivationOwnerMixin,
):
    def seal_context_for_dispatch(
        self,
        session_id: str,
        *,
        turn_id: str,
        execution_id: str,
        model_call_id: str,
        provider_version: str,
        tool_snapshot: Sequence[Mapping[str, object]] = (),
        loss: Iterable[str] = (),
        target_format: str = "chat_completions",
        checkpoint_ns: str = "",
        omitted_ref_ids: Iterable[str] = (),
    ) -> str:
        """在 provider dispatch 前封存 Saver-owned assembly，返回 assembly_id。"""
        checkpoint_ns = self._context_owner_namespace(checkpoint_ns)
        require_key(model_call_id, field="model_call_id")
        creation_key = "dispatch-create:" + sha256_jcs(
            {
                "session_id": session_id,
                "checkpoint_ns": checkpoint_ns,
                "turn_id": turn_id,
                "execution_id": execution_id,
                "model_call_id": model_call_id,
            }
        )
        seal_key = "dispatch-seal:" + creation_key
        plan_id = f"plan-{model_call_id}"
        registered = self._find_runtime_context_plan(session_id, plan_id, checkpoint_ns)
        if registered is None or registered.plan_state == "unsealed":
            plan = self.compose_committed_context_plan(
                session_id,
                plan_id=plan_id,
                tool_snapshot=tool_snapshot,
                checkpoint_ns=checkpoint_ns,
            )
            registered = self.create_context_plan(
                session_id,
                replace(plan, plan_creation_idempotency_key=creation_key),
                checkpoint_ns=checkpoint_ns,
            )
        snapshot = self.seal_context_plan(
            session_id,
            runtime_draft(registered),
            seal_idempotency_key=seal_key,
            turn_id=turn_id,
            execution_id=execution_id,
            model_call_id=model_call_id,
            provider_version=provider_version,
            target_format=target_format,
            loss=tuple(loss),
            omitted_ref_ids=frozenset(omitted_ref_ids),
            request_input_hash=sha256_jcs(
                {"tool_snapshot": [dict(tool) for tool in tool_snapshot]}
            ),
            checkpoint_ns=checkpoint_ns,
        )
        return snapshot.assembly_id

    def project_context_plan_to_messages(
        self,
        session_id: str,
        plan: ContextRequestPlan,
        *,
        checkpoint_ns: str = "",
        include_runtime_notices: bool = False,
        include_summaries: bool = False,
        request_only_content: Mapping[str, object] | None = None,
    ) -> list[BaseMessage]:
        """把 Saver 提供的已提交 plan 临时投影成 LangChain messages。"""
        messages, _, _ = self._project_committed_context_plan(
            session_id,
            plan,
            checkpoint_ns=checkpoint_ns,
            include_runtime_notices=include_runtime_notices,
            include_summaries=include_summaries,
            request_only_content=request_only_content,
        )
        return messages

    def project_context_plan_to_history(
        self,
        session_id: str,
        plan: ContextRequestPlan,
        *,
        checkpoint_ns: str = "",
        include_runtime_notices: bool = False,
        include_summaries: bool = False,
    ) -> list[BaseMessage]:
        """用同一 sealed selection 生成不含 request-only body 的历史消息。

        历史投影不应为了证明 request-only source 存在而读取 detail store，也
        不应把工具定义伪造成 LangChain message。它仍通过同一 assembly owner
        校验 plan/selection，并只读取 selection 中的 canonical item；因此
        Web/诊断历史与 provider/LangChain 请求不会各自重排一套 item。
        """
        messages, _, _ = self._project_committed_context_plan(
            session_id,
            plan,
            checkpoint_ns=checkpoint_ns,
            include_runtime_notices=include_runtime_notices,
            include_summaries=include_summaries,
            include_request_only=False,
        )
        return messages

    def project_context_plan_with_diagnostics(
        self,
        session_id: str,
        plan: ContextRequestPlan,
        *,
        checkpoint_ns: str = "",
        include_runtime_notices: bool = False,
        include_summaries: bool = False,
        request_only_content: Mapping[str, object] | None = None,
    ) -> tuple[list[BaseMessage], tuple[str, ...]]:
        """投影消息并显式返回 provider 不支持的 item capability loss。"""
        messages, _, losses = self._project_committed_context_plan(
            session_id,
            plan,
            checkpoint_ns=checkpoint_ns,
            include_runtime_notices=include_runtime_notices,
            include_summaries=include_summaries,
            request_only_content=request_only_content,
            collect_capability_losses=True,
        )
        return messages, losses

    def project_context_plan_to_provider(
        self,
        session_id: str,
        plan: ContextRequestPlan,
        *,
        target_format: str,
        checkpoint_ns: str = "",
        include_runtime_notices: bool = False,
        include_summaries: bool = False,
        request_only_content: Mapping[str, object] | None = None,
    ) -> tuple[list[BaseMessage], list[dict[str, object]], tuple[str, ...]]:
        """以同一 sealed selection 同时生成消息与独立 provider tools。"""
        self._require_context_plan_owner(session_id, plan)
        return self._project_committed_context_plan(
            session_id,
            plan,
            checkpoint_ns=checkpoint_ns,
            include_runtime_notices=include_runtime_notices,
            include_summaries=include_summaries,
            request_only_content=request_only_content,
            target_format=target_format,
            collect_capability_losses=True,
        )

    def project_context_plan_to_messages_with_evidence(
        self,
        session_id: str,
        plan: ContextRequestPlan,
        *,
        checkpoint_ns: str = "",
        include_runtime_notices: bool = False,
        include_summaries: bool = False,
        request_only_content: Mapping[str, object] | None = None,
    ) -> tuple[list[BaseMessage], ProjectionEvidence]:
        """返回 LangChain message 与同一 Saver selection 的完整证据。"""
        messages, _, losses = self._project_committed_context_plan(
            session_id,
            plan,
            checkpoint_ns=self._context_owner_namespace(checkpoint_ns),
            include_runtime_notices=include_runtime_notices,
            include_summaries=include_summaries,
            request_only_content=request_only_content,
            collect_capability_losses=True,
        )
        evidence = build_projection_evidence(
            self._committed_context_plan(
                session_id,
                plan,
                checkpoint_ns=self._context_owner_namespace(checkpoint_ns),
            ),
            projection="langchain",
            losses=losses,
        )
        return messages, evidence

    def project_context_plan_to_history_with_evidence(
        self,
        session_id: str,
        plan: ContextRequestPlan,
        *,
        checkpoint_ns: str = "",
        include_runtime_notices: bool = False,
        include_summaries: bool = False,
    ) -> tuple[list[BaseMessage], ProjectionEvidence]:
        """返回 Web/history message 与同一 Saver selection 的完整证据。"""
        normalized_ns = self._context_owner_namespace(checkpoint_ns)
        messages, _, losses = self._project_committed_context_plan(
            session_id,
            plan,
            checkpoint_ns=normalized_ns,
            include_runtime_notices=include_runtime_notices,
            include_summaries=include_summaries,
            include_request_only=False,
            collect_capability_losses=True,
        )
        evidence = build_projection_evidence(
            self._committed_context_plan(
                session_id,
                plan,
                checkpoint_ns=normalized_ns,
            ),
            projection="web_history",
            losses=losses,
        )
        return messages, evidence

    def project_context_plan_to_native_with_evidence(
        self,
        session_id: str,
        plan: ContextRequestPlan,
        *,
        checkpoint_ns: str = "",
    ) -> tuple[dict[str, object], ProjectionEvidence]:
        """返回 native request 与同一 Saver selection 的完整证据。"""
        native = self.project_context_plan_to_native(
            session_id,
            plan,
            checkpoint_ns=checkpoint_ns,
        )
        evidence = build_projection_evidence(
            self._committed_context_plan(
                session_id,
                plan,
                checkpoint_ns=self._context_owner_namespace(checkpoint_ns),
            ),
            projection="native",
            losses=native.get("losses", ()),
        )
        if native.get("selection") != list(evidence.selection):
            raise ValueError(
                "plan-order-integrity: native projection selection evidence 不一致"
            )
        return native, evidence

    def project_context_plan_to_provider_with_evidence(
        self,
        session_id: str,
        plan: ContextRequestPlan,
        *,
        target_format: str,
        checkpoint_ns: str = "",
        include_runtime_notices: bool = False,
        include_summaries: bool = False,
        request_only_content: Mapping[str, object] | None = None,
    ) -> tuple[list[BaseMessage], list[dict[str, object]], ProjectionEvidence]:
        """返回 provider/LangChain 双 wire 与同一 selection 证据。"""
        messages, tools, losses = self.project_context_plan_to_provider(
            session_id,
            plan,
            target_format=target_format,
            checkpoint_ns=checkpoint_ns,
            include_runtime_notices=include_runtime_notices,
            include_summaries=include_summaries,
            request_only_content=request_only_content,
        )
        evidence = build_projection_evidence(
            self._committed_context_plan(
                session_id,
                plan,
                checkpoint_ns=self._context_owner_namespace(checkpoint_ns),
            ),
            projection="provider",
            losses=losses,
        )
        return messages, tools, evidence

    def _project_committed_context_plan(
        self,
        session_id: str,
        plan: ContextRequestPlan,
        *,
        checkpoint_ns: str,
        include_runtime_notices: bool = False,
        include_summaries: bool = False,
        request_only_content: Mapping[str, object] | None = None,
        target_format: str | None = None,
        collect_capability_losses: bool = False,
        include_request_only: bool = True,
    ) -> tuple[list[BaseMessage], list[dict[str, object]], tuple[str, ...]]:
        """从同一个已提交 selection 生成所有外部 context projection。

        LangChain history、诊断 view 和 provider dispatch 必须共享同一份
        assembly manifest。这个 owner helper 统一完成 plan identity 校验、
        canonical item 读取、request-only source 恢复以及 ToolSetRef 投影，
        让不同调用方不能各自重新选择 history 或工具。
        """
        committed_plan = self._committed_context_plan(
            session_id,
            plan,
            checkpoint_ns=checkpoint_ns,
        )
        item_ids = [
            entry.ref.ref_id
            for entry in committed_plan.selection
            if entry.included
            and isinstance(entry.ref, ContextRef)
            and entry.ref.ref_type == "canonical_item"
        ]
        resolved_request_content = (
            self._request_content_for_plan(
                session_id,
                checkpoint_ns,
                committed_plan,
                request_only_content,
            )
            if include_request_only
            else {}
        )
        items = self._storage.read_items(
            session_id,
            checkpoint_ns=checkpoint_ns,
            item_ids=item_ids,
        )
        losses: list[str] = []
        if include_request_only:
            messages = project_context_plan(
                committed_plan,
                items,
                include_runtime_notices=include_runtime_notices,
                include_summaries=include_summaries,
                request_only_content=resolved_request_content,
                capability_losses=losses if collect_capability_losses else None,
                include_request_only=True,
            )
        else:
            messages = project_history_plan(
                committed_plan,
                items,
                include_runtime_notices=include_runtime_notices,
                include_summaries=include_summaries,
                capability_losses=losses if collect_capability_losses else None,
            )
        tools: list[dict[str, object]] = []
        if target_format is not None:
            for entry in committed_plan.selection:
                if entry.included and isinstance(entry.ref, ToolSetRef):
                    tools.extend(
                        project_tool_set_ref(entry.ref, target_format=target_format)
                    )
        return messages, tools, tuple(losses)

    def supports_itemized_context(
        self,
        session_id: str,
        *,
        checkpoint_ns: str = "",
    ) -> bool:
        """返回当前 rollout 是否已切换到 v2 item owner。"""
        return self._storage.supports_itemized_context(
            session_id,
            checkpoint_ns=checkpoint_ns,
        )

    def _committed_context_plan(
        self,
        session_id: str,
        plan: ContextRequestPlan,
        *,
        checkpoint_ns: str,
    ) -> ContextRequestPlan:
        """只允许投影 Saver 已提交的 assembly plan。

        ``ContextRequestPlan`` 是不可变值对象，但调用方仍可能自行构造一个
        形式上 sealed 的对象。assembly snapshot 才是持久化授权边界；投影前
        必须从 storage 读回并比较 plan identity/hash/selection，不能让业务层
        以自造的 plan 旁路 SQLite manifest 或 context reader。
        """
        self._require_context_plan_owner(session_id, plan)
        if plan.plan_state != "sealed" or plan.assembly_id is None:
            raise ValueError("context plan 未 sealed，不能进入 projector")
        persisted = self._storage.get_context_assembly(
            session_id,
            assembly_id=plan.assembly_id,
            checkpoint_ns=checkpoint_ns,
        )
        persisted_plan = persisted.as_sealed_plan()
        if (
            persisted.plan_id != plan.plan_id
            or persisted_plan.plan_hash() != plan.plan_hash()
            or persisted.selection != plan.selection
        ):
            raise ValueError(
                "plan-order-integrity: projector plan 与已提交 assembly 不一致"
            )
        # restore/history/retry/rewind/compaction 的每个投影都必须复用已封存的
        # activation snapshot；缺失、owner/hash 不符或正文被 retention 清理时在
        # 这里显式失败，绝不回退当前 URI、文件或 Registry。
        self.read_sealed_resource_activation(
            session_id,
            assembly_id=persisted.assembly_id,
            plan_hash=persisted.plan_hash,
            request_hash=persisted.request_hash,
            checkpoint_ns=checkpoint_ns,
        )
        return persisted_plan

    def get_canonical_item(
        self,
        session_id: str,
        *,
        item_id: str,
        checkpoint_ns: str = "",
    ) -> CanonicalItemRecord | None:
        """读取已提交 item，供 finalization 复用标准 checkpoint 已写入的事实。"""
        items = self._storage.read_items(
            session_id,
            checkpoint_ns=self._context_owner_namespace(checkpoint_ns),
            item_ids=(item_id,),
        )
        return items[0] if items else None

    def read_canonical_items(
        self,
        session_id: str,
        *,
        checkpoint_ns: str = "",
    ) -> tuple[CanonicalItemRecord, ...]:
        """提供已提交 canonical item 的只读 owner API。"""
        return tuple(
            self._storage.read_items(
                session_id,
                checkpoint_ns=self._context_owner_namespace(checkpoint_ns),
            )
        )

    @staticmethod
    def _context_owner_namespace(checkpoint_ns: str) -> str:
        """把 LangGraph model 子 namespace 归一到 rollout context owner。"""
        if checkpoint_ns.startswith("model:"):
            return ""
        return checkpoint_ns


__all__ = ["ContextOwnerMixin"]
