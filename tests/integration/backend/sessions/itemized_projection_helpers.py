"""itemized projection 集成测试的独立 fixture 与结果读取 helper。"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.identifier import create_uuid_hex
from app.core.resource_uri import thread_resource_uri
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.epoch.tool_set import ToolSetSnapshot
from app.domain.itemized.hashing import contribution_content_hash, sha256_jcs
from app.domain.itemized.identity.refs import ContextRef
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.domain.itemized.plans.contributions import ContextContribution, SourceBinding
from app.domain.itemized.plans.request import ContextRequestPlan
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.harness.python.run_context import TestRunContext
from tests.support.fork_deadline import ForkDeadlineRegistry


async def copy_rollout_snapshot(
    saver: RolloutCheckpointSaver, source_session_id: str, target_session_id: str, *,
    fork_deadlines: ForkDeadlineRegistry,
    mode: str = "full_rollout_copy", anchor_item_id: str | None = None,
    relationship: str = "detached", target_thread_id: str | None = None,
):
    """存储专项经真实 preflight/capture/install 复制到 fixture 已创建的空 Thread。"""
    storage = saver._storage.storage
    source_thread_id = storage.main_thread_id(source_session_id)
    target_thread_id = target_thread_id or storage.main_thread_id(target_session_id)
    operation_id = create_uuid_hex(namespace="fork/operation")
    target_workspace_id = storage._path_resolver.catalog_store.get_node(target_session_id).workspace_id
    deadline = fork_deadlines.for_operation(
        target=f"{target_workspace_id}/{target_session_id}", operation_key=operation_id,
    )
    try:
        deadline.set_stage("fork.source_revision_read")
        with storage.read_connection(source_session_id, source_thread_id, "") as connection:
            view, revision = connection.execute(
                "SELECT branch.head_view_id,meta.history_view_revision FROM checkpoint_namespace_state namespace "
                "JOIN branches branch ON branch.branch_id=namespace.active_branch_id "
                "JOIN database_meta meta ON meta.singleton_id=1 WHERE namespace.checkpoint_ns=''",
            ).fetchone()
        source_owner = MutationIntentOwner(source_session_id, source_thread_id)
        point = await deadline.wait(
            saver.forks.source.preflight_fork(
                source_session_id=source_session_id, source_owner=source_owner, mode=mode,
                source_view_id=view, source_view_revision=revision, anchor_item_id=anchor_item_id,
            ),
            "fork.source_preflight",
        )
    except Exception:
        # preflight_fork 只持有来源 read lease，不创建目标或提交 capture。
        if not deadline._timed_out:
            fork_deadlines.complete(deadline)
        raise
    try:
        result = await deadline.wait(
            saver.forks.afork(
                source_session_id=source_session_id, target_session_id=target_session_id,
                source_uri=thread_resource_uri(storage._path_resolver.catalog_store.get_node(source_session_id).workspace_id,
                                               source_session_id, source_thread_id),
                source_owner=source_owner, target_owner=MutationIntentOwner(target_session_id, target_thread_id),
                mode=mode, relationship=relationship, source_preflight=point, fork_id=operation_id,
                source_view_id=point.source_view_id, source_view_revision=point.source_view_revision,
                anchor_item_id=point.anchor_item_id,
            ),
            "fork.checkpoint_afork",
        )
    except Exception:
        # 直接等待的本地协程已失败并退出；真正超时仍由原 deadline 收敛。
        if not deadline._timed_out:
            fork_deadlines.complete(deadline)
        raise
    if result.journal_state != "committed":
        raise RuntimeError(f"fork 未 durable commit: {result.journal_state!r}")
    fork_deadlines.complete(deadline)
    return result


@pytest.fixture(scope="module")
def projection_workspace(request: pytest.FixtureRequest) -> Path:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    shutil.copytree(
        Path.cwd() / "tests/fixtures/workspaces/model_tool_test_workspace",
        context.workspace_root,
        dirs_exist_ok=True,
    )
    return context.workspace_root


@pytest.fixture
def projection_saver(
    request: pytest.FixtureRequest,
    projection_workspace: Path,
    session_bundle_factory: Callable[[Path, str], Path],
) -> Iterator[tuple[RolloutCheckpointSaver, str, Path]]:
    sessions = projection_workspace / ".boxteam/sessions"
    session_id = f"ses_{create_uuid_hex()}"
    session_bundle_factory(sessions, session_id)
    with RolloutCheckpointSaver(
        sessions,
    ) as saver:
        thread = saver._storage.storage.main_thread_id(session_id)
        owner_root = saver._storage.storage._path_resolver.resolve_thread_node(session_id, thread)
        accepted = saver.accept_turn(
            session_id, thread_id=thread,
            accepted_ingress_id="user-1", acceptance_idempotency_key="projection-accept",
            job_id="projection-job", admission_idempotency_key="projection-admission",
            payload="第一条用户输入", turn_id="turn-1", root_item_id="item-user-1",
        )
        checkpoint = empty_checkpoint()
        checkpoint["id"] = "cp-initial"
        checkpoint["channel_values"] = {
            "messages": [
                HumanMessage(
                    content="第一条用户输入",
                    id="user-1",
                    response_metadata={"turn_id": "turn-1"},
                ),
                AIMessage(content="第二条历史输出", id="answer-1"),
            ]
        }
        checkpoint["channel_versions"] = {"messages": "1"}
        admission = saver.execution_admission(
            session_id, thread_id=thread,
            execution_id=str(accepted["initial_execution_id"]),
        )
        with (
            session_thread_owner_scope(
                MutationIntentOwner(session_id, thread), langgraph_thread_id=thread,
            ),
            execution_admission_scope(admission, langgraph_thread_id=thread),
        ):
            saver.put(
                build_checkpoint_config(thread),
                checkpoint,
                {"source": "projection-contract"},
                {"messages": "1"},
            )
            yield saver, session_id, owner_root


@pytest.fixture
def projection_draft(projection_saver):
    saver, session_id, _ = projection_saver
    thread = saver._storage.storage.main_thread_id(session_id)
    body = [{"type": "text", "text": "贡献正文，必须按 contribution identity 恢复"}]
    contribution = ContextContribution(
        contribution_id="contribution-z",
        source_kind="environment",
        source_revision="environment-v1",
        body=body,
        content_hash=contribution_content_hash("prompt", body),
        extensions={} , source_binding=SourceBinding(source_ref='contribution-z'),
    )
    saver.register_context_contribution(session_id, thread, contribution, request_content=body)
    plan = saver.compose_committed_context_plan(
        session_id, thread,
        plan_id=f"plan-{uuid4().hex}",
        tool_set=ToolSetSnapshot(selection_revision=sha256_jcs({"read_file": True}), tools=(
            {
                "tool_id": "read_file",
                "name": "read_file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            },
        )),
    )
    contribution_ref = next(ref for ref in plan.refs if ref.ref_type == "request_only")
    # 只调整 Saver 返回的 draft；正式 selection/ordinal/detail 全由 Saver seal 分配。
    contribution_ref = replace(contribution_ref, ref_id="plan-ref-a")
    plain_body = [
        {"type": "text", "text": "普通 request-only，无 contribution binding"}
    ]
    plain = ContextRef.request_only_ref(
        "plain-ref",
        session_id=session_id, thread_id=thread,
        plan_id=plan.plan_id,
        source_revision="plain-v1",
        content=plain_body,
        payload_kind="structured_content",
        source_ref="source:plain",
    )
    history = tuple(ref for ref in plan.refs if ref.ref_type == "canonical_item")
    plan = replace(plan, refs=(history[1], contribution_ref, plain, history[0]))
    return plan, {"plain-ref": plain_body}


@pytest.fixture
def projection_plan(projection_saver, projection_draft):
    saver, session_id, _ = projection_saver
    plan, bodies = projection_draft
    plan = register_projection_draft(saver, session_id, plan)
    return saver.seal_context_plan(
        session_id, plan.thread_id,
        plan,
        wire_profile=None, wire_params=None, seal_idempotency_key=f"seal:{plan.plan_id}",
        turn_id="turn-1",
        execution_id=saver.execution_for_turn(session_id, thread_id=saver._storage.storage.main_thread_id(session_id), turn_id="turn-1"),
        provider_version="contract-provider",
        request_only_content=bodies,
    )


def register_projection_draft(
    saver: RolloutCheckpointSaver, session_id: str, draft: ContextRequestPlan
) -> ContextRequestPlan:
    """显式注册测试最终 draft；不封存、不补造源正文或已封存历史。"""
    draft = replace(draft, plan_creation_idempotency_key=f"create:{draft.plan_id}")
    registered = saver.create_context_plan(session_id, draft)
    assert registered.plan_state == "unsealed"
    return draft


def _project(saver: RolloutCheckpointSaver, session_id: str, assembly_id: str) -> dict:
    thread = saver._storage.storage.main_thread_id(session_id)
    snapshot = saver.get_context_assembly(session_id, thread, assembly_id=assembly_id)
    plan = snapshot.as_sealed_plan()
    messages, losses = saver.project_context_plan_with_diagnostics(session_id, thread, plan)
    projection = saver.project_context_plan_to_history_with_diagnostics(
        session_id, thread,
        plan,
    )
    history, history_losses = projection.messages, projection.losses
    native = saver.project_context_plan_to_native(session_id, thread, plan)
    return {
        "selection": [entry.to_dict() for entry in plan.selection],
        "plan_hash": plan.plan_hash(),
        "messages": [message.model_dump(mode="json") for message in messages],
        "history": [message.model_dump(mode="json") for message in history],
        "losses": list(losses),
        "history_losses": list(history_losses),
        "native": native,
    }


@pytest.fixture
def overlay_source(projection_saver):
    saver, session_id, _ = projection_saver
    thread_id = saver._storage.storage._path_resolver.main_thread_id(session_id)
    base = [{"type": "text", "text": "AGENTS 稳定 base A"}]
    delta = [{"type": "text", "text": "AGENTS diff A→B"}]
    source = SimpleNamespace(
        session_id=session_id,
        thread_id=thread_id,
        checkpoint_ns="",
        overlay_id="overlay-agents",
        source_kind="workspace_policy",
        source_revision="B",
        source_overlay_epoch=0,
        base_ref="z-base",
        delta_ref="a-delta",
        base_source_revision="A",
        delta_source_revision="B",
        delta_from_revision="A",
        delta_to_revision="B",
        delta_diff_hash=sha256_jcs(delta),
        status="active",
        idempotency_key="overlay-A-B",
    )
    saver.register_source_overlay(
        source,
        session_id=session_id,
        thread_id=thread_id,
        base_content=base,
        delta_content=delta,
    )
    return source
