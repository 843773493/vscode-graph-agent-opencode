"""已注册 draft 的 Saver seal 协调；随机 identity 只能在重试校验之后分配。"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.request_plan import ContextRequestPlan
from app.services.infrastructure.rollout_context.checkpoint.seal.cleanup import (
    record_failed_seal,
    release_uncommitted_details,
)
from app.services.infrastructure.rollout_context.checkpoint.seal.retry import (
    committed_retry,
    require_key,
    require_registered_draft,
    runtime_draft,
    seal_input_hash,
)


def seal_registered_plan(
    owner,
    session_id: str,
    plan: ContextRequestPlan,
    *,
    seal_idempotency_key: str,
    checkpoint_ns: str,
    assembly_options: Mapping[str, object],
    request_only_content: Mapping[str, object] | None,
    request_input_hash: str | None,
    activation_snapshot=None,
) -> ContextAssemblySnapshot:
    require_key(seal_idempotency_key, field="seal_idempotency_key")
    with owner._lock:
        registered = require_registered_draft(owner, session_id, plan, checkpoint_ns)
        draft = runtime_draft(registered)
        # activation binding identity 进入 seal input preimage：重试必须复用同一
        # 冻结 snapshot，不得在同一 seal key 下换成另一个 activation 结果。
        activation_bindings_hash = (
            None
            if activation_snapshot is None
            else f"{activation_snapshot.activation_snapshot_id}|"
            f"{activation_snapshot.bindings_hash}|"
            f"{activation_snapshot.activation_provenance_hash}"
        )
        input_hash = seal_input_hash(
            draft,
            checkpoint_ns,
            assembly_options,
            request_input_hash,
            activation_bindings_hash,
        )
        retry = committed_retry(
            owner,
            registered,
            seal_idempotency_key=seal_idempotency_key,
            input_hash=input_hash,
            checkpoint_ns=checkpoint_ns,
            assembly_options=assembly_options,
            request_only_content=request_only_content,
        )
        if retry is not None:
            # 重试不得重新绑定 activation；已提交 assembly 的 activation 事实
            # 必须与本冻结 snapshot 一致，否则显式拒绝而非覆盖。
            if activation_snapshot is not None:
                owner.verify_resource_activation_binding(
                    session_id,
                    assembly_id=retry.assembly_id,
                    activation_snapshot=activation_snapshot,
                    checkpoint_ns=checkpoint_ns,
                )
            return retry
        # activation 正文必须在 assemble 之前准备：正文写入失败不能留下已完成
        # assemble 的中间状态；绑定闭包只在 assembly_sealed 事务内使用。
        activation_binding = None
        if activation_snapshot is not None:
            _, activation_binding = owner.prepare_resource_activation_binding(
                activation_snapshot, checkpoint_ns=checkpoint_ns
            )
        assembly_id = f"assembly-{uuid4().hex}"
        records = ()
        try:
            bound, records, detail_refs = owner._bind_request_detail_refs(
                session_id,
                checkpoint_ns,
                draft,
                assembly_id=assembly_id,
                omitted_ref_ids=frozenset(assembly_options["omitted_ref_ids"]),
                request_only_content=request_only_content,
            )
            snapshot = owner._composer_for(session_id, checkpoint_ns).assembly(
                plan=bound,
                assembly_id=assembly_id,
                session_id=session_id,
                request_detail_refs=detail_refs,
                **assembly_options,
            )
        except BaseException as error:
            record_failed_seal(
                owner,
                session_id,
                draft.plan_id,
                seal_idempotency_key,
                checkpoint_ns,
                error,
            )
            release_uncommitted_details(owner, session_id, checkpoint_ns, records)
            raise
        try:
            # 低层 owner 负责其 snapshot/header-detail 阶段的失败 control；
            # 外层只释放自己分配的 request details，避免重复记录失败。
            owner.seal_context_assembly(
                snapshot,
                seal_idempotency_key=seal_idempotency_key,
                seal_input_hash=input_hash,
                checkpoint_ns=checkpoint_ns,
                activation_binding=activation_binding,
            )
        except BaseException:
            release_uncommitted_details(owner, session_id, checkpoint_ns, records)
            raise
        return owner._storage.get_context_assembly(
            session_id, assembly_id=assembly_id, checkpoint_ns=checkpoint_ns
        )
