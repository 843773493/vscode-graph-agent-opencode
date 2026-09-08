"""真实 Saver 的冻结 source、omitted 重试和 header detail 丢响应合同。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.domain.itemized.hashing import contribution_content_hash, sha256_jcs
from app.domain.itemized.request_plan import ContextContribution, ContextRequestPlan
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.composer import (
    ContextPlanComposer,
)
from tests.integration.backend.sessions.itemized_projection_helpers import (
    _project,
    register_projection_draft,
)
from tests.integration.backend.sessions.rollout_context.test_schema4_checkpoint_seal import (
    _no_allocation,
    _seal,
    _state,
)
from tests.integration.backend.sessions.rollout_context.test_schema4_checkpoint_seal import (
    draft as draft,  # noqa: PLC0414 - 正式 fixture 按当前 request.node.path 隔离
)
from tests.integration.backend.sessions.rollout_context.test_schema4_checkpoint_seal import (
    registry_db as registry_db,  # noqa: PLC0414 - 不运行被导入文件的用例
)


def test_session_source_revision_cannot_rewrite_frozen_plan_after_restart(registry_db):
    saver, session, connection, accepted = registry_db
    old_body = [{"type": "text", "text": "第一版可替换来源"}]
    original = ContextContribution(
        contribution_id="replaceable-prompt",
        source_kind="workspace_policy",
        source_revision="source-v1",
        content_hash=contribution_content_hash("prompt", old_body),
        body=old_body,
        metadata={"replaceable_source": True},
    )
    saver.register_context_contribution(session, original, request_content=old_body)
    plan = register_projection_draft(
        saver,
        session,
        saver.compose_committed_context_plan(session, plan_id="frozen-source"),
    )
    snapshot = _seal(saver, session, plan, accepted)
    before = _project(saver, session, snapshot.assembly_id)
    new_body = [{"type": "text", "text": "新来源只影响之后的请求"}]
    revised = replace(
        original,
        source_revision="source-v2",
        body=new_body,
        content_length=None,
        content_hash=contribution_content_hash("prompt", new_body),
    )
    saver.register_context_contribution(session, revised, request_content=new_body)
    assert connection.execute(
        "SELECT source_revision FROM context_contributions WHERE contribution_id = ?",
        (original.contribution_id,),
    ).fetchone() == ("source-v2",)
    state = _state(saver, session, connection)
    with RolloutCheckpointSaver(saver._detail_store.sessions_dir) as restarted:
        assert _project(restarted, session, snapshot.assembly_id) == before
        registered = restarted.get_context_plan_registration(
            session, plan_id=plan.plan_id
        )
        assert registered.draft.contributions[0].source_revision == "source-v1"
        assert _seal(restarted, session, registered.draft, accepted) == snapshot
    assert _state(saver, session, connection) == state


def test_omitted_retry_never_reads_supplied_or_persisted_body(
    registry_db, draft, monkeypatch
):
    saver, session, connection, accepted = registry_db
    plan = saver.create_context_plan(session, draft).draft
    options = {"omitted_ref_ids": ("request-ref", "tools")}
    snapshot = _seal(saver, session, plan, accepted, **options)
    before = _state(saver, session, connection)
    with RolloutCheckpointSaver(saver._detail_store.sessions_dir) as restarted:
        monkeypatch.setattr(restarted._detail_store, "read", _no_allocation)
        monkeypatch.setattr(restarted._detail_store, "write", _no_allocation)
        retry = _seal(
            restarted,
            session,
            plan,
            accepted,
            **options,
            request_only_content={"request-ref": object()},
        )
        assert retry == snapshot
    assert _state(saver, session, connection) == before


def test_header_detail_survives_committed_response_loss_and_restart(
    registry_db, monkeypatch
):
    saver, session, connection, accepted = registry_db
    draft = ContextRequestPlan(
        session_id=session,
        plan_id="header-lost",
        refs=(),
        plan_creation_idempotency_key="header-lost-create",
    )
    plan = saver.create_context_plan(session, draft).draft
    snapshot = ContextPlanComposer().assembly(
        plan=plan,
        session_id=session,
        assembly_id="header-lost-assembly",
        turn_id=accepted["turn_id"],
        execution_id=accepted["initial_execution_id"],
        provider_version="header-provider",
    )
    options = {
        "seal_idempotency_key": "header-lost-seal",
        "seal_input_hash": sha256_jcs({"request": "header-lost"}),
        "detail": {"audit": "已提交正文不能删"},
        "required_detail": True,
    }
    original = saver._storage.seal_context_assembly
    commits = []

    def lose_response(*args, **kwargs):
        commits.append(original(*args, **kwargs))
        raise RuntimeError("injected committed header response loss")

    monkeypatch.setattr(saver._storage, "seal_context_assembly", lose_response)
    with pytest.raises(RuntimeError, match="injected committed header response loss"):
        saver.seal_context_assembly(snapshot, **options)
    detail = saver._storage.get_context_assembly_detail_ref(
        session, assembly_id=snapshot.assembly_id
    )
    assert (
        saver.read_context_plan_detail(session, detail_ref=detail)["detail"]
        == options["detail"]
    )
    assert connection.execute(
        "SELECT count(*) FROM context_plan_seal_failures"
    ).fetchone() == (0,)
    before = _state(saver, session, connection)
    with RolloutCheckpointSaver(saver._detail_store.sessions_dir) as restarted:
        monkeypatch.setattr(restarted._detail_store, "write", _no_allocation)
        assert restarted.seal_context_assembly(snapshot, **options) == commits[0]
    assert _state(saver, session, connection) == before
