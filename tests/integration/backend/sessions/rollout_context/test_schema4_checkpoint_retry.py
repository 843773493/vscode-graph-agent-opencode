"""真实 Saver 的冻结 source、omitted 重试和 header detail 丢响应合同。"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace

import pytest

from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import contribution_content_hash, sha256_jcs
from app.domain.itemized.identity.refs import selection_ref_identity
from app.domain.itemized.plans.contributions import ContextContribution
from app.domain.itemized.plans.request import ContextRequestPlan
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.composer import (
    ContextPlanComposer,
)
from app.services.infrastructure.rollout_context.runtime.detail_store import (
    DetailUnavailableError,
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

EMPTY_TOOL_SET = ToolSetSnapshot(selection_revision=sha256_jcs({}), tools=())


@pytest.fixture
def sealed_snapshot_retry(registry_db):
    saver, session, _connection, accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)

    def create(*, with_detail=True, sensitive=False):
        plan = saver.create_context_plan(session, ContextRequestPlan(
            session_id=session, thread_id=thread, plan_id="snapshot-read-once",
            refs=(), plan_creation_idempotency_key="snapshot-read-once:create",
        )).draft
        snapshot = ContextPlanComposer().assembly(
            plan=plan, session_id=session, thread_id=thread,
            assembly_id="snapshot-read-once:assembly", turn_id=accepted["turn_id"],
            execution_id=accepted["initial_execution_id"], provider_version="header-provider",
        )
        options = {
            "seal_idempotency_key": "snapshot-read-once:seal",
            "seal_input_hash": sha256_jcs("snapshot-read-once:input"),
            "detail": {"audit": "original"} if with_detail else None,
            "required_detail": with_detail,
            "sensitive_detail": sensitive,
        }
        commit = saver.seal_context_assembly(session, thread, snapshot, **options)
        return snapshot, options, commit

    return create


@pytest.mark.parametrize("with_detail", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_snapshot_retry_reads_assembly_and_detail_in_one_fresh_authority_snapshot(
    registry_db, sealed_snapshot_retry, monkeypatch, with_detail, restart,
):
    saver, session, connection, _accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    snapshot, options, commit = sealed_snapshot_retry(with_detail=with_detail)
    before = _state(saver, session, connection)
    with (RolloutCheckpointSaver(saver._detail_store.sessions_dir) if restart else nullcontext(saver)) as reader:
        audits = []
        snapshots = []
        open_snapshot = reader._storage.storage.open_read_snapshot

        def observe_snapshot(*args, **kwargs):
            snapshots.append(kwargs.get("validate_integrity", False))
            return open_snapshot(*args, **kwargs)

        monkeypatch.setattr(reader._storage.storage, "open_read_snapshot", observe_snapshot)
        maintenance = reader._storage.storage._maintenance_owner
        validate = maintenance._validate_v2_commit_offsets

        def observe(*args, **kwargs):
            audits.append(kwargs.get("validate_jsonl_items", True))
            return validate(*args, **kwargs)

        monkeypatch.setattr(maintenance, "_validate_v2_commit_offsets", observe)
        monkeypatch.setattr(reader._detail_store, "prepare", _no_allocation)
        for attempt in range(2):
            assert reader.seal_context_assembly(session, thread, snapshot, **options) == commit
            # 注册读取、assembly/detail 的同次读取、写前恢复各一次，后续重试仍 fresh。
            print(f"snapshot retry audits: detail={with_detail}, restart={restart}, attempt={attempt}, audits={audits}")
            assert audits == [True] * (attempt + 1)
            assert snapshots == [False, False] * (attempt + 1)
    assert _state(saver, session, connection) == before


@pytest.mark.parametrize("damage", ["body", "manifest", "flags", "sensitive"])
def test_snapshot_retry_still_revalidates_detail_and_permissions_after_success(
    registry_db, sealed_snapshot_retry, damage,
):
    saver, session, connection, _accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    snapshot, options, commit = sealed_snapshot_retry(sensitive=damage == "sensitive")
    if damage == "sensitive":
        before = _state(saver, session, connection)
        with pytest.raises(PermissionError, match="未授权读取"):
            saver.seal_context_assembly(session, thread, snapshot, **options)
    else:
        assert saver.seal_context_assembly(session, thread, snapshot, **options) == commit
        _, detail_ref = saver._storage.assemblies.read_context_assembly(
            session, thread, assembly_id=snapshot.assembly_id,
        )
        if damage == "body":
            path = saver._detail_store._files.path(detail_ref)
            assert path is not None
            raw = path.read_bytes()
            changed = raw.replace(b'"audit":"original"', b'"audit":"modified"')
            assert changed != raw and len(changed) == len(raw)
            path.write_bytes(changed)
        elif damage == "manifest":
            connection.execute("UPDATE context_plan_details SET sensitive=1")
            connection.commit()
        else:
            connection.execute("UPDATE context_plan_details SET required=0")
            connection.commit()
        before = _state(saver, session, connection)
        error_type = ValueError if damage == "flags" else DetailUnavailableError
        message = {
            "body": "content manifest hash/length 不一致",
            "manifest": "sensitive 与 protection 不一致",
            "flags": "重试 header detail manifest 不一致",
        }[damage]
        with pytest.raises(error_type, match=message):
            saver.seal_context_assembly(session, thread, snapshot, **options)
        if damage == "body":
            assert path.read_bytes() == changed
    assert _state(saver, session, connection) == before


def test_session_source_revision_cannot_rewrite_frozen_plan_after_restart(registry_db):
    saver, session, connection, accepted = registry_db
    old_body = [{"type": "text", "text": "第一版可替换来源"}]
    original = ContextContribution(
        contribution_id="replaceable-prompt",
        source_kind="workspace_policy",
        source_revision="source-v1",
        content_hash=contribution_content_hash("prompt", old_body),
        body=old_body,
        # 可替换 source slot 由 typed core 字段声明；metadata 同名 key
        # 不再参与替换判定（旧路径已物理下线）。
        replacement_policy="replaceable",
    )
    saver.register_context_contribution(
        session,
        saver._storage.storage.main_thread_id(session),
        original,
        request_content=old_body,
    )
    plan = register_projection_draft(
        saver,
        session,
        saver.compose_committed_context_plan(
            session,
            saver._storage.storage.main_thread_id(session),
            plan_id="frozen-source",
            tool_set=EMPTY_TOOL_SET,
        ),
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
    frozen_state = _state(saver, session, connection)
    with pytest.raises(ValueError, match="已封存，禁止更新原 registry slot"):
        saver.register_context_contribution(
            session,
            plan.thread_id,
            revised,
            request_content=new_body,
            expected_source_revision="source-v1",
        )
    assert _state(saver, session, connection) == frozen_state
    # 已封存revision身份不可复写；后续请求显式登记新revision identity。
    revised = replace(
        revised,
        contribution_id="replaceable-prompt:source-v2",
        replacement_policy="immutable",
    )
    saver.register_context_contribution(
        session,
        plan.thread_id,
        revised,
        request_content=new_body,
    )
    assert connection.execute(
        "SELECT source_revision FROM context_contributions WHERE contribution_id = ?",
        (original.contribution_id,),
    ).fetchone() == ("source-v1",)
    assert connection.execute(
        "SELECT source_revision FROM context_contributions WHERE contribution_id = ?",
        (revised.contribution_id,),
    ).fetchone() == ("source-v2",)
    following = saver.compose_committed_context_plan(
        session,
        plan.thread_id,
        plan_id="following-source",
        tool_set=EMPTY_TOOL_SET,
    )
    following = register_projection_draft(
        saver,
        session,
        replace(
            following,
            refs=tuple(
                ref for ref in following.refs if ref.ref_id != original.contribution_id
            ),
            contributions=tuple(
                contribution
                for contribution in following.contributions
                if contribution.contribution_id == revised.contribution_id
            ),
        ),
    )
    newer = _seal(
        saver,
        session,
        following,
        accepted,
        seal_idempotency_key="following-source-seal",
    )
    assert newer.contributions[0].source_revision == "source-v2"
    assert "新来源只影响之后的请求" in str(
        _project(saver, session, newer.assembly_id)["messages"]
    )
    state = _state(saver, session, connection)
    with RolloutCheckpointSaver(saver._detail_store.sessions_dir) as restarted:
        assert _project(restarted, session, snapshot.assembly_id) == before
        registered = restarted.get_context_plan_registration(
            session,
            plan_id=plan.plan_id,
            thread_id=restarted._storage.storage.main_thread_id(session),
        )
        assert registered.draft.contributions[0].source_revision == "source-v1"
        assert _seal(restarted, session, registered.draft, accepted) == snapshot
    assert _state(saver, session, connection) == state


def test_omitted_retry_never_reads_supplied_or_persisted_body(
    registry_db, draft, monkeypatch
):
    saver, session, connection, accepted = registry_db
    plan = saver.create_context_plan(session, draft).draft
    options = {"omitted_ref_identities": tuple(
        selection_ref_identity(ref) for ref in (*plan.refs, *plan.tool_set_refs)
    )}
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
        thread_id=saver._storage.storage.main_thread_id(session),
    )
    plan = saver.create_context_plan(session, draft).draft
    snapshot = ContextPlanComposer().assembly(
        plan=plan,
        session_id=session,
        assembly_id="header-lost-assembly",
        turn_id=accepted["turn_id"],
        execution_id=accepted["initial_execution_id"],
        provider_version="header-provider",
        thread_id=saver._storage.storage.main_thread_id(session),
    )
    options = {
        "seal_idempotency_key": "header-lost-seal",
        "seal_input_hash": sha256_jcs({"request": "header-lost"}),
        "detail": {"audit": "已提交正文不能删"},
        "required_detail": True,
    }
    original = saver._storage.assemblies.seal_context_assembly
    commits = []

    def lose_response(*args, **kwargs):
        commits.append(original(*args, **kwargs))
        raise RuntimeError("injected committed header response loss")

    monkeypatch.setattr(saver._storage.assemblies, "seal_context_assembly", lose_response)
    with pytest.raises(RuntimeError, match="injected committed header response loss"):
        saver.seal_context_assembly(
            session, saver._storage.storage.main_thread_id(session), snapshot, **options
        )
    _, detail = saver._storage.assemblies.read_context_assembly(
        session,
        saver._storage.storage.main_thread_id(session),
        assembly_id=snapshot.assembly_id,
    )
    assert (
        saver.read_context_plan_detail(
            session, saver._storage.storage.main_thread_id(session), detail_ref=detail
        )["detail"]
        == options["detail"]
    )
    assert connection.execute(
        "SELECT count(*) FROM context_plan_seal_failures"
    ).fetchone() == (0,)
    before = _state(saver, session, connection)
    with RolloutCheckpointSaver(saver._detail_store.sessions_dir) as restarted:
        monkeypatch.setattr(restarted._detail_store, "write", _no_allocation)
        assert (
            restarted.seal_context_assembly(
                session, saver._storage.storage.main_thread_id(session), snapshot, **options
            )
            == commits[0].commit_id
        )
    assert _state(saver, session, connection) == before
