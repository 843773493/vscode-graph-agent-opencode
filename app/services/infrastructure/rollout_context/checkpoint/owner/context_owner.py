"""RolloutCheckpointSaver 的 context owner 与 provider projection 边界。

本 mixin 只封装 Saver 对已提交 context plan/assembly 的 ownership、detail
和 projector 调用；checkpoint/fork persistence 仍由 Saver/persistence owner
提供，避免业务层旁路 RolloutContextStore。
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from langchain_core.messages import BaseMessage, message_to_dict, messages_from_dict

from app.agents.providers.wire_finalize import WIRE_PROJECTOR_ID, WIRE_PROJECTOR_VERSION
from app.domain.itemized.dispatch_ref import SealedContextDispatchRef
from app.domain.itemized.identity.refs import ToolSetRef
from app.domain.itemized.plans.request import ContextRequestPlan
from app.domain.itemized.records import CanonicalItemRecord
from app.services.infrastructure.rollout_context.checkpoint.owner.composition import (
    ContextPlanCompositionMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.owner.context_details import (
    ContextDetailOwnerMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.owner.context_dispatch import (
    ContextDispatchOwnerMixin,
)
from app.services.infrastructure.rollout_context.checkpoint.owner.context_projection import (
    ContextProjectionInput,
    ContextProjectionOwnerMixin,
    ContextProjectionResult,
)
from app.services.infrastructure.rollout_context.checkpoint.owner.resource_activation import (
    ResourceActivationOwnerMixin,
)
from app.services.infrastructure.rollout_context.provider.toolset_request_bridge import (
    project_tool_set_ref,
)
from app.services.infrastructure.rollout_context.storage.primitives import (
    RolloutReadSnapshot,
)
from app.services.mapping.itemized.history import project_history_plan
from app.services.mapping.itemized.langchain import project_context_plan
from app.services.mapping.itemized.projection import (
    ProjectionEvidence,
    build_projection_evidence,
)
from app.services.mapping.itemized.resource_activation import (
    project_sealed_resource_refs,
)


class ContextOwnerMixin(
    ContextDispatchOwnerMixin,
    ContextDetailOwnerMixin,
    ContextPlanCompositionMixin,
    ContextProjectionOwnerMixin,
    ResourceActivationOwnerMixin,
):
    def project_context_plan_to_messages(
        self,
        session_id: str,
        thread_id: str,
        plan: ContextRequestPlan,
        *,
        checkpoint_ns: str = "",
        include_runtime_notices: bool = False,
        include_summaries: bool = False,
        request_only_content: Mapping[str, object] | None = None,
    ) -> list[BaseMessage]:
        """把 Saver 提供的已提交 plan 临时投影成 LangChain messages。"""
        projection = self._project_committed_context_plan(
            session_id,
            thread_id,
            plan,
            checkpoint_ns=checkpoint_ns,
            include_runtime_notices=include_runtime_notices,
            include_summaries=include_summaries,
            request_only_content=request_only_content,
        )
        return projection.messages

    def project_context_plan_to_history(
        self,
        session_id: str,
        thread_id: str,
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
        projection = self._project_committed_context_plan(
            session_id,
            thread_id,
            plan,
            checkpoint_ns=checkpoint_ns,
            include_runtime_notices=include_runtime_notices,
            include_summaries=include_summaries,
            include_request_only=False,
        )
        return projection.messages

    def project_context_plan_with_diagnostics(
        self,
        session_id: str,
        thread_id: str,
        plan: ContextRequestPlan,
        *,
        checkpoint_ns: str = "",
        include_runtime_notices: bool = False,
        include_summaries: bool = False,
        request_only_content: Mapping[str, object] | None = None,
    ) -> tuple[list[BaseMessage], tuple[str, ...]]:
        """投影消息并显式返回 provider 不支持的 item capability loss。"""
        projection = self._project_committed_context_plan(
            session_id,
            thread_id,
            plan,
            checkpoint_ns=checkpoint_ns,
            include_runtime_notices=include_runtime_notices,
            include_summaries=include_summaries,
            request_only_content=request_only_content,
            collect_capability_losses=True,
        )
        return projection.messages, projection.losses

    def project_context_plan_to_provider(
        self,
        session_id: str,
        thread_id: str,
        plan: ContextRequestPlan,
        *,
        target_format: str,
        checkpoint_ns: str = "",
        include_runtime_notices: bool = False,
        include_summaries: bool = False,
        request_only_content: Mapping[str, object] | None = None,
    ) -> tuple[list[BaseMessage], list[dict[str, object]], tuple[str, ...]]:
        """按 sealed selection 投影当前目标的消息与 tools；历史 wire 由专用读取入口返回。"""
        projection = self._project_committed_context_plan(
            session_id,
            thread_id,
            plan,
            checkpoint_ns=checkpoint_ns,
            include_runtime_notices=include_runtime_notices,
            include_summaries=include_summaries,
            request_only_content=request_only_content,
            target_format=target_format,
            collect_capability_losses=True,
        )
        return projection.messages, projection.tools, projection.losses

    def read_sealed_context_wire(
        self,
        session_id: str,
        thread_id: str,
        plan: ContextRequestPlan,
        *,
        checkpoint_ns: str = "",
    ) -> dict[str, object]:
        """读取该次实际封存的请求，不以当前附件 alias 或 Provider 配置重新编码。

        fork 保留来源 assembly；即使来源已是另一次 fork，历史请求仍使用当时
        的目标 URI。scope、canonical、detail、prefix 与 activation 沿原冷读链
        校验，正文复用 assembly 已保存的规范化 wire，不建立另一套消息历史。
        """
        committed, items, content = self._read_committed_context_projection(
            session_id, thread_id, plan,
            checkpoint_ns=self.context_owner_namespace(checkpoint_ns),
        )
        snapshot = committed.assembly
        # 纯映射核对封存 selection/prefix 与正文，不查询当前可变附件目录。
        project_context_plan(
            committed.plan, items,
            include_runtime_notices=True, include_summaries=True,
            request_only_content=content, seal_binding=snapshot.seal_binding,
        )
        wire = snapshot.request_hash_preimage
        if (snapshot.projector_id != WIRE_PROJECTOR_ID
                or snapshot.target_format not in {"chat_completions", "responses"}
                or not isinstance(wire, dict)):
            raise ValueError("detail-unavailable: assembly 未封存完整 Provider wire")
        # snapshot 的公开 getter 已隔离可变 JSON；调用方不能改写封存正文。
        return wire

    def project_context_plan_for_dispatch(
        self,
        session_id: str,
        thread_id: str,
        plan: ContextRequestPlan,
        *,
        target_format: str,
        checkpoint_ns: str = "",
    ) -> ContextProjectionResult:
        """从一次已提交读取生成本次 dispatch 的 provider 与 native 投影。"""
        return self._project_committed_context_plan(
            session_id, thread_id, plan, checkpoint_ns=checkpoint_ns,
            include_runtime_notices=True, include_summaries=True,
            target_format=target_format, collect_capability_losses=True,
            include_native=target_format == "responses",
        )

    def project_context_plan_to_messages_with_evidence(
        self,
        session_id: str,
        thread_id: str,
        plan: ContextRequestPlan,
        *,
        checkpoint_ns: str = "",
        include_runtime_notices: bool = False,
        include_summaries: bool = False,
        request_only_content: Mapping[str, object] | None = None,
    ) -> tuple[list[BaseMessage], ProjectionEvidence]:
        """返回 LangChain message 与同一 Saver selection 的完整证据。"""
        projection = self._project_committed_context_plan(
            session_id,
            thread_id,
            plan,
            checkpoint_ns=self.context_owner_namespace(checkpoint_ns),
            include_runtime_notices=include_runtime_notices,
            include_summaries=include_summaries,
            request_only_content=request_only_content,
            collect_capability_losses=True,
        )
        return projection.messages, projection.evidence("langchain")

    def project_context_plan_to_history_with_evidence(
        self,
        session_id: str,
        thread_id: str,
        plan: ContextRequestPlan,
        *,
        checkpoint_ns: str = "",
        include_runtime_notices: bool = False,
        include_summaries: bool = False,
    ) -> tuple[list[BaseMessage], ProjectionEvidence]:
        """返回 Web/history message 与同一 Saver selection 的完整证据。"""
        normalized_ns = self.context_owner_namespace(checkpoint_ns)
        projection = self._project_committed_context_plan(
            session_id,
            thread_id,
            plan,
            checkpoint_ns=normalized_ns,
            include_runtime_notices=include_runtime_notices,
            include_summaries=include_summaries,
            include_request_only=False,
            collect_capability_losses=True,
        )
        return projection.messages, projection.evidence("web_history")

    def project_context_plan_to_native_with_evidence(
        self,
        session_id: str,
        thread_id: str,
        plan: ContextRequestPlan,
        *,
        checkpoint_ns: str = "",
    ) -> tuple[dict[str, object], ProjectionEvidence]:
        """返回 native request 与同一 Saver selection 的完整证据。"""
        committed, items, content = self._read_committed_context_projection(
            session_id, thread_id, plan,
            checkpoint_ns=self.context_owner_namespace(checkpoint_ns),
        )
        resources = (project_sealed_resource_refs(committed.resource_activation)
                     if committed.resource_activation is not None else ())
        native = self._project_native_context(
            session_id, thread_id, committed, items, content,
            resource_refs=resources,
        )
        evidence = build_projection_evidence(
            committed.plan, projection="native",
            losses=native["losses"], resource_refs=resources,
        )
        if native["selection"] != list(evidence.selection):
            raise ValueError("plan-order-integrity: native projection selection evidence 不一致")
        return native, evidence

    def project_context_plan_to_provider_with_evidence(
        self,
        session_id: str,
        thread_id: str,
        plan: ContextRequestPlan,
        *,
        target_format: str,
        checkpoint_ns: str = "",
        include_runtime_notices: bool = False,
        include_summaries: bool = False,
        request_only_content: Mapping[str, object] | None = None,
    ) -> tuple[list[BaseMessage], list[dict[str, object]], ProjectionEvidence]:
        """返回 provider/LangChain 双 wire 与同一 selection 证据。"""
        projection = self._project_committed_context_plan(
            session_id, thread_id, plan, target_format=target_format,
            checkpoint_ns=self.context_owner_namespace(checkpoint_ns),
            include_runtime_notices=include_runtime_notices,
            include_summaries=include_summaries,
            request_only_content=request_only_content,
            collect_capability_losses=True,
        )
        return projection.messages, projection.tools, projection.evidence("provider")

    def _project_committed_context_plan(
        self,
        session_id: str,
        thread_id: str,
        expected: ContextRequestPlan | SealedContextDispatchRef,
        *,
        checkpoint_ns: str,
        include_runtime_notices: bool = False,
        include_summaries: bool = False,
        request_only_content: Mapping[str, object] | None = None,
        target_format: str | None = None,
        collect_capability_losses: bool = False,
        include_request_only: bool = True,
        include_native: bool = False,
    ) -> ContextProjectionResult:
        """从同一个已提交 selection 生成所有外部 context projection。

        LangChain history、诊断 view 和 provider dispatch 必须共享同一份
        assembly manifest。这个 owner helper 统一完成 plan identity 校验、
        canonical item 读取、request-only source 恢复以及 ToolSetRef 投影，
        让不同调用方不能各自重新选择 history 或工具。
        """
        committed_snapshot, items, resolved_request_content = self._read_committed_context_projection(
            session_id, thread_id, expected, checkpoint_ns=checkpoint_ns,
            request_only_content=request_only_content, include_request_only=include_request_only,
        )
        return self._project_context_projection(
            session_id, thread_id, committed_snapshot, items, resolved_request_content,
            include_runtime_notices=include_runtime_notices, include_summaries=include_summaries,
            target_format=target_format, collect_capability_losses=collect_capability_losses,
            include_request_only=include_request_only, include_native=include_native,
        )

    def _project_context_projection(
        self,
        session_id: str,
        thread_id: str,
        context: ContextProjectionInput,
        items: Mapping[tuple[str, str, str], CanonicalItemRecord],
        resolved_request_content: Mapping[str, object],
        *,
        include_runtime_notices: bool,
        include_summaries: bool,
        target_format: str | None,
        collect_capability_losses: bool = True,
        include_request_only: bool = True,
        include_native: bool = False,
    ) -> ContextProjectionResult:
        """只映射当前 seal owner 或冷读取 owner 已验证的材料，不读取持久化。"""
        # 正文解析、LangChain/Native 投影共用本次已构造的 plan；不跨操作缓存。
        committed_snapshot = context.assembly
        committed_plan = context.plan
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
                seal_binding=committed_snapshot.seal_binding,
            )
        else:
            messages = project_history_plan(
                committed_plan,
                items,
                include_runtime_notices=include_runtime_notices,
                include_summaries=include_summaries,
                capability_losses=losses if collect_capability_losses else None,
            )
        messages = self.project_message_attachment_refs(session_id, thread_id, messages)
        resources = (project_sealed_resource_refs(context.resource_activation)
                     if context.resource_activation is not None else ())
        native = self._project_native_context(
            session_id, thread_id, context, items, resolved_request_content,
            resource_refs=resources,
        ) if include_native else None
        tools: list[dict[str, object]] = []
        if target_format == "responses" and native is not None:
            # 同次 Responses 只编码一次工具；返回副本保持两份投影可独立编辑。
            tools = deepcopy(native["request"]["tools"])
        elif target_format is not None:
            for entry in committed_plan.selection:
                if entry.included and isinstance(entry.ref, ToolSetRef):
                    tools.extend(
                        project_tool_set_ref(entry.ref, target_format=target_format)
                    )
        return ContextProjectionResult(
            assembly=committed_snapshot, messages=messages, tools=tools,
            losses=tuple(losses), native=native, resource_refs=resources,
            plan=committed_plan,
        )

    def project_message_attachment_refs(self, session_id: str, thread_id: str, messages: list[BaseMessage]) -> list[BaseMessage]:
        """所有派生请求/历史复用附件投影 owner 的 target-local mapping，canonical 不改写。"""
        return messages_from_dict([
            self._storage.attachment_projection.project_message_attachments(session_id, thread_id, message_to_dict(message))
            for message in messages
        ])

    def supports_itemized_context(
        self,
        session_id: str,
        thread_id: str,
        *,
        checkpoint_ns: str = "",
    ) -> bool:
        """返回当前 rollout 是否已切换到 v2 item owner。"""
        return self._storage.storage.supports_itemized_context(
            session_id,
            thread_id,
            checkpoint_ns=self.context_owner_namespace(checkpoint_ns),
        )

    def _committed_context_snapshot(
        self,
        expected: ContextRequestPlan | SealedContextDispatchRef | str,
        *,
        snapshot: RolloutReadSnapshot,
    ) -> ContextProjectionInput:
        """从当前快照读取 assembly，并核对调用方提供的 plan 或派发引用。

        ``ContextRequestPlan`` 是不可变值对象，但调用方仍可能自行构造一个
        形式上 sealed 的对象。assembly snapshot 才是持久化授权边界；投影前
        必须从 storage 读回并比较 plan identity/hash/selection，不能让业务层
        以自造的 plan 旁路 SQLite manifest 或 context reader。
        """
        session_id, thread_id, checkpoint_ns = snapshot.session_id, snapshot.thread_id, snapshot.checkpoint_ns
        if isinstance(expected, ContextRequestPlan):
            self._require_context_plan_read_owner(
                session_id, thread_id, expected, checkpoint_ns, snapshot=snapshot,
            )
        with self._storage.storage.read_connection(
            session_id, thread_id, checkpoint_ns, snapshot=snapshot,
        ) as connection:
            persisted, _ = self._storage.assemblies._read_context_assembly_connection(
                connection, session_id, thread_id,
                assembly_id=expected if isinstance(expected, str) else expected.assembly_id,
                checkpoint_ns=checkpoint_ns,
            )
        if isinstance(expected, SealedContextDispatchRef):
            # 冷派发引用在同一次权威读取中核对全部 binding，再读取 activation 和正文。
            # 不先读 assembly 再拿其 plan 重开快照，避免重复审计和重复展开 registry。
            expected.validate_snapshot(persisted)
        elif isinstance(expected, ContextRequestPlan) and (
            persisted.plan_id != expected.plan_id
            or persisted.plan_hash != expected.plan_hash()
            or persisted.selection != expected.selection
        ):
            raise ValueError(
                "plan-order-integrity: projector plan 与已提交 assembly 不一致"
            )
        activation = None
        # 正式 wire v2 投影必须复用 sealed activation；纯 plan v1 保持自身合同。
        if (persisted.projector_id, persisted.projector_version) == (
            WIRE_PROJECTOR_ID, WIRE_PROJECTOR_VERSION,
        ):
            activation = self.load_sealed_resource_activation(
                session_id,
                thread_id=thread_id,
                assembly_id=persisted.assembly_id,
                plan_hash=persisted.plan_hash,
                request_hash=persisted.request_hash,
                checkpoint_ns=checkpoint_ns,
                snapshot=snapshot,
            )
        return ContextProjectionInput(persisted, persisted.as_sealed_plan(), activation)

    def get_canonical_item(
        self,
        session_id: str,
        *,
        thread_id: str,
        item_id: str,
        checkpoint_ns: str = "",
    ) -> CanonicalItemRecord | None:
        """读取已提交 item，供 finalization 复用标准 checkpoint 已写入的事实。"""
        items = self._storage.storage.read_items(
            session_id,
            thread_id,
            checkpoint_ns=self.context_owner_namespace(checkpoint_ns),
            item_ids=(item_id,),
        )
        return items[0] if items else None

    def read_canonical_items(
        self,
        session_id: str,
        thread_id: str,
        *,
        checkpoint_ns: str = "",
    ) -> tuple[CanonicalItemRecord, ...]:
        """按精确 SessionThread 提供已提交 canonical item 的只读 API。"""
        return tuple(
            self._storage.storage.read_items(
                session_id,
                thread_id,
                checkpoint_ns=self.context_owner_namespace(checkpoint_ns),
            )
        )

    @staticmethod
    def context_owner_namespace(checkpoint_ns: str) -> str:
        """把 LangGraph model 子 namespace 归一到 rollout context owner。"""
        if checkpoint_ns.startswith("model:"):
            return ""
        return checkpoint_ns


__all__ = ["ContextOwnerMixin"]
