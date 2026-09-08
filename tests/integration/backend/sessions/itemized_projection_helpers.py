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
from app.domain.itemized.hashing import contribution_content_hash, sha256_jcs
from app.domain.itemized.refs import ContextRef
from app.domain.itemized.request_plan import ContextContribution, ContextRequestPlan
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.harness.python.run_context import TestRunContext


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
    projection_workspace: Path,
    session_bundle_factory: Callable[[Path, str], Path],
) -> Iterator[tuple[RolloutCheckpointSaver, str, Path]]:
    sessions = projection_workspace / ".boxteam/sessions"
    session_id = f"ses_projection_{uuid4().hex}"
    session = session_bundle_factory(sessions, session_id)
    with RolloutCheckpointSaver(sessions) as saver:
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
        saver.put(
            build_checkpoint_config(session_id),
            checkpoint,
            {"source": "projection-contract"},
            {"messages": "1"},
        )
        yield saver, session_id, session


@pytest.fixture
def projection_draft(projection_saver):
    saver, session_id, _ = projection_saver
    body = [{"type": "text", "text": "贡献正文，必须按 contribution identity 恢复"}]
    contribution = ContextContribution(
        contribution_id="contribution-z",
        source_kind="environment",
        source_revision="environment-v1",
        body=body,
        content_hash=contribution_content_hash("prompt", body),
        metadata={"source_ref": "contribution-z"},
    )
    saver.register_context_contribution(session_id, contribution, request_content=body)
    plan = saver.compose_committed_context_plan(
        session_id,
        plan_id=f"plan-{uuid4().hex}",
        tool_snapshot=(
            {
                "tool_id": "read_file",
                "name": "read_file",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            },
        ),
    )
    contribution_ref = next(ref for ref in plan.refs if ref.ref_type == "request_only")
    # 只调整 Saver 返回的 draft；正式 selection/ordinal/detail 全由 Saver seal 分配。
    contribution_ref = replace(contribution_ref, ref_id="plan-ref-a")
    plain_body = [
        {"type": "text", "text": "普通 request-only，无 contribution binding"}
    ]
    plain = ContextRef.request_only_ref(
        "plain-ref",
        session_id=session_id,
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
        session_id,
        plan,
        seal_idempotency_key=f"seal:{plan.plan_id}",
        turn_id="turn-1",
        execution_id=saver.execution_for_turn(session_id, turn_id="turn-1"),
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
    assert registered.registration_origin == "runtime"
    return draft


def _project(saver: RolloutCheckpointSaver, session_id: str, assembly_id: str) -> dict:
    snapshot = saver.get_context_assembly(session_id, assembly_id=assembly_id)
    plan = snapshot.as_sealed_plan()
    messages, losses = saver.project_context_plan_with_diagnostics(session_id, plan)
    history, history_losses = saver.project_context_plan_to_history_with_diagnostics(
        session_id,
        plan,
    )
    native = saver.project_context_plan_to_native(session_id, plan)
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
    base = [{"type": "text", "text": "AGENTS 稳定 base A"}]
    delta = [{"type": "text", "text": "AGENTS diff A→B"}]
    source = SimpleNamespace(
        session_id=session_id,
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
    saver.register_source_overlay(source, base_content=base, delta_content=delta)
    return source
