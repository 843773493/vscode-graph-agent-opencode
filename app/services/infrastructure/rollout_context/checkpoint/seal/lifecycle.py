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
) -> ContextAssemblySnapshot:
    require_key(seal_idempotency_key, field="seal_idempotency_key")
    with owner._lock:
        registered = require_registered_draft(owner, session_id, plan, checkpoint_ns)
        draft = runtime_draft(registered)
        input_hash = seal_input_hash(
            draft, checkpoint_ns, assembly_options, request_input_hash
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
            return retry
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
            )
        except BaseException:
            release_uncommitted_details(owner, session_id, checkpoint_ns, records)
            raise
        return owner._storage.get_context_assembly(
            session_id, assembly_id=assembly_id, checkpoint_ns=checkpoint_ns
        )
