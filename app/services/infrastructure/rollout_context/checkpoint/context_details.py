"""Context assembly detail 的 durable owner。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.detail_ref import DetailRef
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
    detail_record_from_mapping,
)


class ContextDetailOwnerMixin:
    """只负责 detail 文件的 seal、读取、权限和 retention GC。"""

    def seal_context_plan(
        self,
        session_id: str,
        plan: ContextRequestPlan,
        *,
        turn_id: str,
        execution_id: str,
        provider_version: str,
        seal_idempotency_key: str,
        model_call_id: str | None = None,
        target_format: str = "responses",
        loss: Iterable[str] = (),
        request_input_hash: str | None = None,
        request_only_content: Mapping[str, object] | None = None,
        omitted_ref_ids: Iterable[str] = (),
        checkpoint_ns: str = "",
        activation_snapshot: ResourceActivationSnapshotRef | None = None,
    ) -> ContextAssemblySnapshot:
        """只封存显式注册的 draft；重复请求在分配 detail 之前校验并返回。

        ``activation_snapshot`` 是 activation coordinator 冻结的内存 snapshot；
        它必须与 assembly 在同一次 ``assembly_sealed`` 提交原子绑定。为 None 时
        不写 activation 行（尚未接入 activation 的调用方）。
        """
        self._require_context_plan_owner(session_id, plan)
        return seal_registered_plan(
            self,
            session_id,
            plan,
            seal_idempotency_key=seal_idempotency_key,
            checkpoint_ns=self._context_owner_namespace(checkpoint_ns),
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
            activation_snapshot=activation_snapshot,
        )

    def seal_context_assembly(
        self,
        snapshot: ContextAssemblySnapshot,
        *,
        seal_idempotency_key: str,
        seal_input_hash: str,
        detail: Mapping[str, object] | None = None,
        required_detail: bool = False,
        sensitive_detail: bool = False,
        checkpoint_ns: str = "",
        activation_binding=None,
    ) -> int:
        """在 provider dispatch 前完成 snapshot 与可选详情的 sealed 提交。"""
        return seal_snapshot(
            self,
            snapshot,
            seal_idempotency_key=seal_idempotency_key,
            seal_input_hash=seal_input_hash,
            detail=detail,
            required_detail=required_detail,
            sensitive_detail=sensitive_detail,
            checkpoint_ns=self._context_owner_namespace(checkpoint_ns),
            activation_binding=activation_binding,
        )

    def read_context_plan_detail(
        self,
        session_id: str,
        *,
        detail_ref: DetailRef,
        include_sensitive: bool = False,
        checkpoint_ns: str = "",
    ) -> dict[str, object]:
        """通过 Saver 读取 assembly detail，执行 session/敏感权限边界。"""
        if not isinstance(detail_ref, DetailRef):
            raise TypeError("source-mismatch: detail_ref 必须是 typed DetailRef")
        detail_ref.require_owner(session_id)
        raw = self._storage.get_context_plan_detail(
            session_id,
            detail_ref=detail_ref,
            checkpoint_ns=checkpoint_ns,
        )
        record = detail_record_from_mapping(raw)
        return self._detail_store.read(
            session_id=session_id,
            record=record,
            include_sensitive=include_sensitive,
        )

    def gc_context_plan_details(
        self,
        session_id: str,
        *,
        expired_before: datetime,
        checkpoint_ns: str = "",
    ) -> tuple[DetailRef, ...]:
        """只清理未被 sealed assembly 引用的过期 detail。"""
        candidates = self._storage.list_expired_context_plan_details(
            session_id,
            expired_before=expired_before,
            checkpoint_ns=checkpoint_ns,
        )
        removable = tuple(
            detail_ref
            for detail_ref, assembly_id in candidates
            if not self._storage.context_assembly_references_detail(
                session_id,
                assembly_id=assembly_id,
                detail_ref=detail_ref,
                checkpoint_ns=checkpoint_ns,
            )
        )
        # 先提交可恢复 tombstone；标记失败时绝不能先删除正文。若进程在
        # 标记后崩溃，registry 的 GC candidates 必须允许重试物理清理。
        self._storage.mark_context_plan_details_unavailable(
            session_id,
            detail_refs=removable,
            checkpoint_ns=checkpoint_ns,
        )
        return self._detail_store.gc(
            session_id=session_id,
            expired_before=expired_before,
            allowed_refs=removable,
        )


__all__ = ["ContextDetailOwnerMixin"]
