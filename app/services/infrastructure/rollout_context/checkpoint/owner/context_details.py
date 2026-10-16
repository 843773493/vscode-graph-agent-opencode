"""Context assembly detail 的 durable owner。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime

from app.agents.providers.wire_finalize import ChatWireProfile, ResponsesWireProfile
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.epoch.seal_binding import ContextSealPreparation
from app.domain.itemized.identity.detail_ref import DetailRef
from app.domain.itemized.request_plan import ContextRequestPlan
from app.domain.itemized.resource_activation import (
    ResourceActivationSnapshotRef,
)
from app.services.infrastructure.rollout_context.checkpoint.seal.assembly import (
    seal_snapshot,
)
from app.services.infrastructure.rollout_context.checkpoint.seal.lifecycle import (
    seal_registered_plan,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    PreparedDetail,
    detail_record_from_mapping,
)


class ContextDetailOwnerMixin:
    """只负责 detail 文件的 seal、读取、权限和 retention GC。"""

    def seal_context_plan(
        self,
        session_id: str,
        thread_id: str,
        plan: ContextRequestPlan,
        *,
        turn_id: str | None,
        execution_id: str,
        provider_version: str,
        wire_profile: ChatWireProfile | ResponsesWireProfile | None,
        wire_params: Mapping[str, object] | None,
        seal_idempotency_key: str,
        model_call_id: str | None = None,
        target_format: str = "responses",
        loss: Iterable[str] = (),
        request_input_hash: str | None = None,
        request_only_content: Mapping[str, object] | None = None,
        omitted_ref_ids: Iterable[str] = (),
        checkpoint_ns: str = "",
        activation_snapshot: ResourceActivationSnapshotRef | None = None,
        context_preparation: ContextSealPreparation | None = None,
    ) -> ContextAssemblySnapshot:
        """只封存显式注册的 draft；重复请求在分配 detail 之前校验并返回。

        正式 wire seal 必须提供当前 admission 的真实 model-call UUID 与
        coordinator 已冻结的 activation snapshot，并在同一次 assembly 事务
        原子绑定。显式不投影 wire 的低层 plan seal 保持纯 plan 合同。
        """
        self._require_context_plan_owner(session_id, thread_id, plan)
        return seal_registered_plan(
            self,
            session_id,
            thread_id,
            plan,
            seal_idempotency_key=seal_idempotency_key,
            checkpoint_ns=self.context_owner_namespace(checkpoint_ns),
            assembly_options={
                "turn_id": turn_id,
                "execution_id": execution_id,
                "provider_version": provider_version,
                "model_call_id": model_call_id,
                "target_format": target_format,
                "loss": tuple(loss),
                "omitted_ref_ids": frozenset(omitted_ref_ids),
            },
            request_only_content=request_only_content,
            request_input_hash=request_input_hash,
            wire_profile=wire_profile,
            wire_params=wire_params,
            activation_snapshot=activation_snapshot,
            context_preparation=context_preparation,
        ).snapshot

    def seal_context_assembly(
        self,
        session_id: str,
        thread_id: str,
        snapshot: ContextAssemblySnapshot,
        *,
        seal_idempotency_key: str,
        seal_input_hash: str,
        detail: Mapping[str, object] | None = None,
        required_detail: bool = False,
        sensitive_detail: bool = False,
        checkpoint_ns: str = "",
        activation_binding=None,
        prepared_details: tuple[PreparedDetail, ...] = (),
    ) -> int:
        """在 provider dispatch 前完成 snapshot 与可选详情的 sealed 提交。"""
        if (snapshot.session_id, snapshot.thread_id) != (session_id, thread_id):
            raise ValueError("source-mismatch: assembly seal owner 不一致")
        receipt, _projection = seal_snapshot(
            self,
            session_id,
            thread_id,
            snapshot,
            registered=self.get_context_plan_registration(
                session_id, thread_id=thread_id, plan_id=snapshot.plan_id,
                checkpoint_ns=checkpoint_ns,
            ),
            seal_idempotency_key=seal_idempotency_key,
            seal_input_hash=seal_input_hash,
            detail=detail,
            required_detail=required_detail,
            sensitive_detail=sensitive_detail,
            checkpoint_ns=self.context_owner_namespace(checkpoint_ns),
            wire=None,
            activation_binding=activation_binding,
            prepared_details=prepared_details,
        )
        return receipt.commit_id

    def read_context_plan_detail(
        self,
        session_id: str,
        thread_id: str,
        *,
        detail_ref: DetailRef,
        include_sensitive: bool = False,
        checkpoint_ns: str = "",
    ) -> dict[str, object]:
        """以当前目标 storage owner 读取详情；DetailRef 保留原历史 scope。

        正文由目标本地 binding 与详情 store 解析，不把请求正文详情误作整体 assembly 审计详情。
        返回 detail store 校验后的 JSON envelope 映射，其中 ``detail`` 是正文，其余
        字段是 envelope 元数据；不是 SQLite registry manifest，也不是裸正文。
        """
        if not isinstance(detail_ref, DetailRef):
            raise TypeError("source-mismatch: detail_ref 必须是 typed DetailRef")
        raw = self._storage.assemblies.get_context_plan_detail(
            session_id,
            thread_id,
            detail_ref=detail_ref,
            checkpoint_ns=checkpoint_ns,
        )
        record = detail_record_from_mapping(raw)
        return self._detail_store.read(
            session_id=session_id,
            thread_id=thread_id,
            record=record,
            include_sensitive=include_sensitive,
        )

    def gc_context_plan_details(
        self,
        session_id: str,
        thread_id: str,
        *,
        expired_before: datetime,
        checkpoint_ns: str = "",
    ) -> tuple[DetailRef, ...]:
        """只清理未被 sealed assembly 引用的过期 detail。"""
        candidates = self._storage.assemblies.list_expired_context_plan_details(
            session_id,
            thread_id,
            expired_before=expired_before,
            checkpoint_ns=checkpoint_ns,
        )
        removable = tuple(
            detail_ref
            for detail_ref, assembly_id in candidates
            if not self._storage.assemblies.context_assembly_references_detail(
                session_id,
                thread_id,
                assembly_id=assembly_id,
                detail_ref=detail_ref,
                checkpoint_ns=checkpoint_ns,
            )
        )
        # 先提交可恢复 tombstone；标记失败时绝不能先删除正文。若进程在
        # 标记后崩溃，registry 的 GC candidates 必须允许重试物理清理。
        self._storage.assemblies.mark_context_plan_details_unavailable(
            session_id,
            thread_id,
            detail_refs=removable,
            checkpoint_ns=checkpoint_ns,
        )
        return self._detail_store.gc(
            session_id=session_id,
            thread_id=thread_id,
            expired_before=expired_before,
            allowed_refs=removable,
        )


__all__ = ["ContextDetailOwnerMixin"]
