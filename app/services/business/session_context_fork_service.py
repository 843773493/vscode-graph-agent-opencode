from __future__ import annotations

import hashlib
import json
from typing import Literal
from uuid import uuid4

from app.core.key_lock_pool import KeyLockPool
from app.core.session_catalog_store import SessionCreationRecord
from app.core.session_catalog_store.contracts import SessionGenerationOriginDTO
from app.core.session_creation import compute_session_creation_preimage_hash
from app.domain.itemized.hashing import canonical_json_bytes
from app.schemas.internal_v2.session import SessionDTO
from app.services.business.session_service import SessionService
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)


class SessionContextForkService:
    """协调会话节点创建，rollout 数据由 Saver 统一物化。"""

    def __init__(
        self,
        *,
        session_service: SessionService,
        checkpointer: RolloutCheckpointSaver,
    ) -> None:
        self._session_service = session_service
        self._checkpointer = checkpointer
        self._key_locks = KeyLockPool()

    async def fork(
        self,
        source_session_id: str,
        *,
        source_thread_id: str | None = None,
        generation_origin: SessionGenerationOriginDTO | None = None,
        title: str | None = None,
        parent_node_id: str | None = None,
        place_under_source: bool = False,
        pinned: bool = False,
        mode: Literal[
            "context_fork", "history_prefix_fork", "full_rollout_copy"
        ] = "context_fork",
        turn_id: str | None = None,
        anchor_mode: Literal["inclusive", "before"] = "inclusive",
        checkpoint_id: str | None = None,
        anchor: str | None = None,
        checkpoint_ns: str = "",
    ) -> SessionDTO:
        physical_parent_node_id = (
            source_session_id if place_under_source else parent_node_id
        )
        # 冻结调用方请求，不把隐式 latest 解析出的 source checkpoint 当成意图。
        request_intent: dict[str, object] = {
            "source_session_id": source_session_id,
            "source_thread_id": source_thread_id,
            "title": title or None,
            "parent_node_id": physical_parent_node_id,
            "pinned": pinned,
            "mode": mode,
            "turn_id": turn_id,
            "anchor_mode": anchor_mode,
            "checkpoint_id": checkpoint_id,
            "anchor": anchor,
            "checkpoint_ns": checkpoint_ns,
        }
        creation_key = (
            "context-fork-generation:" + hashlib.sha256(
                canonical_json_bytes(generation_origin.model_dump(mode="json"))
            ).hexdigest()
            if generation_origin is not None else f"context-fork-{uuid4().hex}"
        )
        async with self._key_locks.lock_for(creation_key):
            if generation_origin is not None:
                try:
                    existing = self._session_service.path_resolver.catalog_store.get_creation_record(creation_key)
                except KeyError:
                    existing = None
                if existing is not None:
                    metadata = json.loads(existing.session_metadata_json)
                    if (existing.purpose != "context_fork"
                        or metadata["context_source_session_id"] != source_session_id
                        or metadata["generation_origin"] != generation_origin.model_dump(mode="json")):
                        raise RuntimeError("generation fork 稳定 key 与冻结创建来源不一致")
                    frozen_intent = (
                        json.loads(existing.request_intent_json)
                        if existing.request_intent_json is not None else None
                    )
                    if frozen_intent != request_intent:
                        raise RuntimeError("generation fork 请求意图冲突（同 key 不同意图，拒绝复用）")
                    if compute_session_creation_preimage_hash(
                        workspace_id=existing.workspace_id,
                        parent_node_id=existing.parent_node_id,
                        title=existing.display_name,
                        title_source=existing.title_source,
                        purpose=existing.purpose,
                        graph_binding=existing.graph_binding,
                        session_metadata=metadata,
                        request_intent=request_intent,
                    ) != existing.preimage_hash:
                        raise RuntimeError("generation fork durable creation preimage 与冻结请求不一致")
                    if existing.state == "published":
                        return await self._session_service.get(existing.session_id)
                    if existing.state == "preparing":
                        proof = await self._checkpointer.forks.recovery.recover_prepared_fork_completion(existing)
                        if proof is not None:
                            return await self._session_service.publish_prepared_context_fork(existing, fork_completion=proof)
                        await self._abort_if_unpublished(existing, "generation fork prepared 未完成，已回滚")
                        raise RuntimeError("generation fork prepared 未完成，须使用新 generation run 重试")
            source_session = await self._session_service.get(source_session_id)
            source_thread_id = source_thread_id or await self._session_service.resolve_main_thread(source_session_id)
            self._session_service.path_resolver.resolve_thread_node(source_session_id, source_thread_id)
            source_owner = self._checkpointer.forks.source.resolve_thread_owner(source_session_id, source_thread_id)
            # context/history fork 的规范要求在创建目标 session/catalog 之前
            # 拒绝运行态 Turn、无 completed anchor 和非法 source checkpoint。
            # 预检只读 source，不创建 target journal；afork 随后会再次解析并在
            # target-local materialization 中提交同一份 source 合同。
            source_preflight = await self._checkpointer.forks.source.preflight_fork(
                source_session_id=source_session_id,
                source_owner=source_owner,
                mode=mode,
                turn_id=turn_id,
                anchor_mode=anchor_mode,
                checkpoint_id=checkpoint_id,
                anchor=anchor,
                checkpoint_ns=checkpoint_ns,
            )
            physical_parent_session_id = (
                source_session_id
                if place_under_source
                else self._session_service.path_resolver.nearest_session_ancestor(
                    parent_node_id
                )
            )
            child_session, creation_record = await self._session_service.prepare_context_fork(
                idempotency_key=creation_key,
                title=title or f"{source_session.title}（上下文副本）",
                agent_id=source_session.current_agent_id,
                parent_session_id=physical_parent_session_id,
                context_source_session_id=source_session_id,
                generation_origin=generation_origin,
                parent_node_id=physical_parent_node_id,
                request_intent=request_intent,
            )

            if creation_record.state == "published":
                return await self._session_service.get(creation_record.session_id)

            try:
                target_owner = self._checkpointer.forks.source.resolve_prepared_main_thread_owner(
                    creation_record
                )
                fork_id = uuid4().hex
                materialization_id = uuid4().hex
                if pinned:
                    self._checkpointer.forks.recovery.begin_pinned_fork_retention_claim(
                        fork_id=fork_id,
                        source_session_id=source_session_id,
                        target_session_id=child_session.session_id,
                    )
                result = await self._checkpointer.forks.afork(
                    source_session_id=source_session_id,
                    target_session_id=child_session.session_id,
                    source_owner=source_owner,
                    target_owner=target_owner,
                    creation_record=creation_record,
                    fork_id=fork_id,
                    materialization_id=materialization_id,
                    mode=mode,
                    turn_id=turn_id,
                    anchor_mode=anchor_mode,
                    checkpoint_id=checkpoint_id,
                    anchor=anchor,
                    relationship="pinned" if pinned else "detached",
                    checkpoint_ns=checkpoint_ns,
                    source_preflight=source_preflight,
                )
                proof = self._checkpointer.forks.recovery.read_committed_fork_completion_proof(
                    record=creation_record,
                    source_session_id=source_session_id,
                    source_owner=source_owner,
                    result=result,
                    mode=mode,
                    relationship="pinned" if pinned else "detached",
                    checkpoint_ns=checkpoint_ns,
                )
            except BaseException:
                await self._abort_if_unpublished(creation_record, "context fork capture/materialization failed")
                raise
            try:
                return await self._session_service.publish_prepared_context_fork(
                    creation_record,
                    fork_completion=proof,
                )
            except BaseException:
                await self._abort_if_unpublished(creation_record, "context fork publish failed")
                raise

    async def _abort_if_unpublished(self, record: SessionCreationRecord, reason: str) -> None:
        """catalog CAS 已成功时，通知失败也不得倒删 target 或释放其引用。"""
        current = self._session_service.path_resolver.catalog_store.get_creation_record(
            record.session_creation_idempotency_key
        )
        if current.state != "preparing":
            return
        self._checkpointer.forks.recovery.release_prepared_fork_retentions(current)
        await self._session_service.abort_prepared_context_fork(current, reason)

    async def recover_prepared_forks(self) -> int:
        """启动时定点回滚 prepared；完整 committed journal 补 proof 后发布。"""
        recovered = 0
        for record in self._session_service.list_preparing_creation_records(purpose="context_fork"):
            async with self._key_locks.lock_for(record.session_creation_idempotency_key):
                try:
                    proof = await self._checkpointer.forks.recovery.recover_prepared_fork_completion(record)
                    if proof is None:
                        await self._abort_if_unpublished(record, "startup fork prepared 未完成，已回滚")
                    else:
                        await self._session_service.publish_prepared_context_fork(record, fork_completion=proof)
                except BaseException:
                    await self._abort_if_unpublished(record, "startup fork recovery failed")
                    raise
                recovered += 1
        return recovered


__all__ = ["SessionContextForkService"]
