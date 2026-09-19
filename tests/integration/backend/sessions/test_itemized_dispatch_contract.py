"""经真实 LangChain dispatch、Provider TCP HTTP 和后端 history API 的合同。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from app.agents.itemized_context_middleware import SealedAssemblyDispatchBridge
from app.agents.providers.openai_responses import BoxteamOpenAIResponsesModel
from app.agents.sealed_assembly_dispatch import read_sealed_native_projection
from app.core.checkpoint_config import build_checkpoint_config
from app.core.job_context import reset_current_job_id, set_current_job_id
from app.domain.itemized.detail_ref import DetailRef
from app.domain.itemized.hashing import canonical_json_bytes
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from app.services.infrastructure.rollout_context.runtime.detail_manifest import (
    detail_relative_path,
)
from tests.integration.backend.sessions.itemized_dispatch_helpers import (
    dispatch_saver,
    invoke_native_dispatch,
    native_dispatch_workspace,
    native_http_server,
    seed_dispatch_history,
    seed_dispatch_overlay,
)
from tests.integration.backend.sessions.itemized_projection_helpers import (
    register_projection_draft,
)

__all__ = ["dispatch_saver", "native_dispatch_workspace", "native_http_server"]


@tool
def inspect_file(path: str) -> str:
    """返回文件检查目标，用于验证工具调用合同。"""
    return f"inspected:{path}"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_native_dispatch_sends_sealed_selection_over_real_http(
    dispatch_saver, native_http_server, mode
) -> None:
    saver, session_id, turn_id, sessions = dispatch_saver
    state, endpoint = native_http_server
    model = BoxteamOpenAIResponsesModel(
        model="openai/gpt-4.1",
        api_key=state.api_key,
        api_base=endpoint,
        custom_llm_provider="openai",
        streaming=True,
        max_retries=0,
    )
    agent = create_agent(
        model,
        tools=[inspect_file],
        system_prompt="HTTP system prompt",
        middleware=[SealedAssemblyDispatchBridge(checkpointer=saver)],
        checkpointer=saver,
    )
    token = set_current_job_id(turn_id)
    try:
        if mode == "sync":
            result = agent.invoke({"messages": []}, build_checkpoint_config(session_id))
        else:
            result = await agent.ainvoke(
                {"messages": []}, build_checkpoint_config(session_id)
            )
    finally:
        reset_current_job_id(token)
    assert "native-http-result" in json.dumps(result["messages"][-1].content)
    assert read_sealed_native_projection() is None
    assert len(state.requests) == 1
    (snapshot,) = saver.list_context_assemblies(session_id)
    with RolloutCheckpointSaver(sessions) as restarted:
        restored = restarted.get_context_assembly(
            session_id, assembly_id=snapshot.assembly_id
        )
        projection = restarted.project_context_plan_to_native(
            session_id, restored.as_sealed_plan()
        )
    body = state.requests[0]["body"]
    for entry, encoded in zip(restored.selection, projection["selection"], strict=True):
        assert "detail_ref" not in encoded["ref"]
        if entry.detail_ref is not None:
            assert isinstance(entry.detail_ref, DetailRef)
            entry.detail_ref.require_owner(session_id, restored.assembly_id)
            assert encoded["detail_ref"] == entry.detail_ref.to_dict()
    assert body["input"] == projection["request"]["input"]
    assert body["tools"] == projection["request"]["tools"]
    # prepare 自己决定 prompt/history 的位置；HTTP 必须忠实消费它的 selection。
    assert [source["plan_ordinal"] for source in projection["wire_sources"]] == list(
        range(len(snapshot.selection) - 1)
    )
    assert sum(item["role"] == "system" for item in body["input"]) == 3
    assert [item["content"][0]["text"] for item in body["input"][-2:]] == [
        "HTTP overlay base",
        "HTTP overlay delta",
    ]
    assert body["tools"][0]["name"] == "inspect_file"
    assert body["tools"][0]["parameters"]["type"] == "object"
    assert body["tools"][0]["parameters"]["properties"]["path"]["type"] == "string"
    assert (
        not {"selection", "plan_hash", "assembly_id", "native_projection"} & body.keys()
    )


@pytest.mark.asyncio
async def test_concurrent_native_dispatch_does_not_share_context(
    dispatch_saver, native_http_server, session_bundle_factory
) -> None:
    saver, first_session, turn_id, sessions = dispatch_saver
    second_session = f"ses_{uuid4().hex}"
    session_bundle_factory(sessions, second_session)
    seed_dispatch_history(saver, second_session)
    seed_dispatch_overlay(saver, second_session)
    state, endpoint = native_http_server

    async def dispatch(session_id: str) -> None:
        model = BoxteamOpenAIResponsesModel(
            model="openai/gpt-4.1",
            api_key=state.api_key,
            api_base=endpoint,
            custom_llm_provider="openai",
            streaming=True,
            max_retries=0,
        )
        agent = create_agent(
            model,
            system_prompt=session_id,
            middleware=[SealedAssemblyDispatchBridge(checkpointer=saver)],
            checkpointer=saver,
        )
        token = set_current_job_id(turn_id)
        try:
            await agent.ainvoke({"messages": []}, build_checkpoint_config(session_id))
        finally:
            reset_current_job_id(token)

    await asyncio.gather(dispatch(first_session), dispatch(second_session))
    assert len(state.requests) == 2
    for session_id in (first_session, second_session):
        (snapshot,) = saver.list_context_assemblies(session_id)
        expected = saver.project_context_plan_to_native(
            session_id, snapshot.as_sealed_plan()
        )
        matching = [
            request["body"]
            for request in state.requests
            if session_id in json.dumps(request["body"], ensure_ascii=False)
        ]
        assert len(matching) == 1
        assert matching[0]["input"] == expected["request"]["input"]
    assert read_sealed_native_projection() is None


@pytest.mark.asyncio
async def test_native_http_tool_loop_replays_canonical_call_and_result(
    dispatch_saver, native_http_server
) -> None:
    saver, session_id, turn_id, sessions = dispatch_saver
    state, endpoint = native_http_server
    state.call_tool = True
    model = BoxteamOpenAIResponsesModel(
        model="openai/gpt-4.1",
        api_key=state.api_key,
        api_base=endpoint,
        custom_llm_provider="openai",
        streaming=True,
        max_retries=0,
    )
    agent = create_agent(
        model,
        tools=[inspect_file],
        system_prompt="HTTP tool loop",
        middleware=[SealedAssemblyDispatchBridge(checkpointer=saver)],
        checkpointer=saver,
    )
    token = set_current_job_id(turn_id)
    try:
        result = await agent.ainvoke(
            {"messages": []}, build_checkpoint_config(session_id)
        )
    finally:
        reset_current_job_id(token)
    assert "native-http-result" in json.dumps(result["messages"][-1].content)
    assert len(state.requests) == 2
    with RolloutCheckpointSaver(sessions) as restarted:
        snapshots = restarted.list_context_assemblies(session_id)
        assert len(snapshots) == 2
        projections = [
            restarted.project_context_plan_to_native(
                session_id, snapshot.as_sealed_plan()
            )
            for snapshot in snapshots
        ]
    for request in state.requests:
        assert request["body"]["input"] in [
            projection["request"]["input"] for projection in projections
        ]
    second_input = state.requests[1]["body"]["input"]
    (call,) = [item for item in second_input if item.get("type") == "function_call"]
    (output,) = [
        item for item in second_input if item.get("type") == "function_call_output"
    ]
    assert call["call_id"] == output["call_id"] == "call-native"
    assert json.loads(call["arguments"]) == {"path": "README.md"}
    assert output["output"] == "inspected:README.md"
    assert second_input.index(call) < second_input.index(output)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_missing_saver_owner_fails_before_provider_http(
    dispatch_saver, native_http_server, mode
) -> None:
    saver, session_id, turn_id, _ = dispatch_saver
    state, endpoint = native_http_server
    model = BoxteamOpenAIResponsesModel(
        model="openai/gpt-4.1",
        api_key=state.api_key,
        api_base=endpoint,
        custom_llm_provider="openai",
        streaming=True,
        max_retries=0,
    )
    # 缺少强制 owner 是错误配置，不以替身伪造 prepare/supports 成功。
    agent = create_agent(
        model,
        middleware=[SealedAssemblyDispatchBridge(checkpointer=None)],
        checkpointer=saver,
    )
    token = set_current_job_id(turn_id)
    try:
        with pytest.raises(TypeError, match="Saver prepare/supports"):
            if mode == "sync":
                agent.invoke({"messages": []}, build_checkpoint_config(session_id))
            else:
                await agent.ainvoke(
                    {"messages": []}, build_checkpoint_config(session_id)
                )
    finally:
        reset_current_job_id(token)
    assert state.requests == []
    assert saver.list_context_assemblies(session_id) == ()
    assert read_sealed_native_projection() is None


@pytest.mark.asyncio
async def test_provider_parameters_cannot_override_sealed_input(
    dispatch_saver, native_http_server
) -> None:
    saver, session_id, turn_id, _ = dispatch_saver
    state, endpoint = native_http_server
    model = BoxteamOpenAIResponsesModel(
        model="openai/gpt-4.1",
        api_key=state.api_key,
        api_base=endpoint,
        custom_llm_provider="openai",
        streaming=True,
        max_retries=0,
        model_kwargs={"instructions": "不能覆盖 sealed prompt"},
    )
    agent = create_agent(
        model,
        system_prompt="sealed prompt",
        middleware=[SealedAssemblyDispatchBridge(checkpointer=saver)],
        checkpointer=saver,
    )
    token = set_current_job_id(turn_id)
    try:
        with pytest.raises(ValueError, match="source-mismatch.*instructions"):
            await agent.ainvoke({"messages": []}, build_checkpoint_config(session_id))
    finally:
        reset_current_job_id(token)
    assert state.requests == []
    assert read_sealed_native_projection() is None


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize(
    "port", ["provider", "native", "messages", "history", "bind", "seal"]
)
def test_foreign_session_plan_is_rejected_without_io(
    dispatch_saver, session_bundle_factory, monkeypatch, empty, port
) -> None:
    saver, source_id, turn_id, sessions = dispatch_saver
    target_id = f"ses_{uuid4().hex}"
    target = session_bundle_factory(sessions, target_id)
    draft = saver.compose_committed_context_plan(
        source_id, plan_id=f"guard-{uuid4().hex}"
    )
    if empty:
        draft = replace(draft, refs=(), contributions=(), tool_set_refs=())
    if port not in {"bind", "seal"}:
        draft = register_projection_draft(saver, source_id, draft)
        draft = saver.seal_context_plan(
            source_id,
            draft,
            seal_idempotency_key=f"seal:{draft.plan_id}",
            turn_id=turn_id,
            execution_id=saver.execution_for_turn(source_id, turn_id=turn_id),
            provider_version="guard-contract",
        ).as_sealed_plan()
    before = {
        str(path.relative_to(target)): path.read_bytes() if path.is_file() else None
        for path in target.rglob("*")
    }
    source_assemblies = saver.list_context_assemblies(source_id)
    io_calls = []

    def reject_io(*args, **kwargs):
        io_calls.append((args, kwargs))
        raise AssertionError("跨 session owner guard 前不得读写 storage/detail")

    # 先经真实 Saver seal/read 取得 plan，再在 I/O 边界设置哨兵；错误 owner
    # 必须在 source lookup 前拒绝，不能只依靠目标文件最终未变化来推断。
    with monkeypatch.context() as io_patch:
        for owner, names in (
            (
                saver._storage,
                (
                    "get_context_assembly",
                    "read_items",
                    "get_context_plan_detail",
                    "register_context_plan_detail",
                ),
            ),
            (saver._detail_store, ("read", "write", "remove")),
        ):
            for name in names:
                io_patch.setattr(owner, name, reject_io)
        with pytest.raises(ValueError, match="source-mismatch.*session owner"):
            if port == "provider":
                saver.project_context_plan_to_provider(
                    target_id, draft, target_format="responses"
                )
            elif port == "native":
                saver.project_context_plan_to_native(target_id, draft)
            elif port == "messages":
                saver.project_context_plan_to_messages(target_id, draft)
            elif port == "history":
                saver.project_context_plan_to_history_with_diagnostics(target_id, draft)
            elif port == "bind":
                saver._bind_request_detail_refs(
                    target_id, "", draft, assembly_id="foreign-assembly"
                )
            else:
                saver.seal_context_plan(
                    target_id,
                    draft,
                    seal_idempotency_key=f"seal:{draft.plan_id}",
                    turn_id=turn_id,
                    execution_id="foreign-execution",
                    provider_version="guard-contract",
                )
    assert {
        str(path.relative_to(target)): path.read_bytes() if path.is_file() else None
        for path in target.rglob("*")
    } == before
    assert io_calls == []
    assert not (target / "rollout/index.sqlite").exists()
    assert saver.list_context_assemblies(source_id) == source_assemblies


@pytest.mark.asyncio
async def test_new_process_restores_overlay_for_real_native_http_dispatch(
    dispatch_saver, native_http_server
) -> None:
    saver, session_id, turn_id, sessions = dispatch_saver
    state, endpoint = native_http_server
    await invoke_native_dispatch(saver, session_id, turn_id, endpoint, state.api_key)
    (first,) = saver.list_context_assemblies(session_id)
    result = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-c",
            (
                "import asyncio,sys; from pathlib import Path; "
                "from app.services.infrastructure.rollout_context.checkpoint.saver import RolloutCheckpointSaver; "
                "from tests.integration.backend.sessions.itemized_dispatch_helpers import invoke_native_dispatch; "
                "asyncio.run(invoke_native_dispatch(RolloutCheckpointSaver(Path(sys.argv[1])), *sys.argv[2:])); "
                "print('restarted-dispatch-ok')"
            ),
            str(sessions),
            session_id,
            turn_id,
            endpoint,
            state.api_key,
        ],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )
    (state.artifacts / "restart.stdout.log").write_text(result.stdout, encoding="utf-8")
    (state.artifacts / "restart.stderr.log").write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert "restarted-dispatch-ok" in result.stdout
    assert len(state.requests) == 2
    with RolloutCheckpointSaver(sessions) as restarted:
        snapshots = restarted.list_context_assemblies(session_id)
        (second,) = [
            snapshot
            for snapshot in snapshots
            if snapshot.assembly_id != first.assembly_id
        ]
        assert second.history_view_revision > first.history_view_revision
        assert second.source_overlay_epoch == first.source_overlay_epoch
        assert (
            restarted.get_context_assembly(session_id, assembly_id=first.assembly_id)
            == first
        )
        expected = restarted.project_context_plan_to_native(
            session_id, second.as_sealed_plan()
        )
    assert state.requests[1]["body"]["input"] == expected["request"]["input"]
    assert (
        state.requests[0]["body"]["input"][-2:]
        == state.requests[1]["body"]["input"][-2:]
    )
    old_overlay = [
        entry for entry in first.selection if entry.base_delta_role != "none"
    ]
    new_overlay = [
        entry for entry in second.selection if entry.base_delta_role != "none"
    ]
    for old, new in zip(old_overlay, new_overlay, strict=True):
        assert old.ref.ref_id == new.ref.ref_id
        assert old.contribution_ordinal == new.contribution_ordinal
        assert old.content_hash == new.content_hash
        assert old.detail_ref != new.detail_ref


@pytest.mark.asyncio
async def test_history_http_matches_inflight_dispatch_canonical_view(
    integration_client, integration_workspace_root_path, native_http_server
) -> None:
    response = await integration_client.post(
        "/api/v1/sessions", json={"title": "native HTTP history"}
    )
    assert response.status_code == 200, response.text
    session_id = response.json()["data"]["session_id"]
    sessions = Path(integration_workspace_root_path) / ".boxteam/sessions"
    state, endpoint = native_http_server
    with RolloutCheckpointSaver(sessions) as saver:
        turn_id = seed_dispatch_history(saver, session_id)
        seed_dispatch_overlay(saver, session_id)
        state.release_response.clear()
        dispatch = asyncio.create_task(
            invoke_native_dispatch(saver, session_id, turn_id, endpoint, state.api_key)
        )
        try:
            assert await asyncio.to_thread(state.request_ready.wait, 20)
            (snapshot,) = saver.list_context_assemblies(session_id)
            expected = saver.project_context_plan_to_history(
                session_id, snapshot.as_sealed_plan()
            )
            response = await integration_client.post(
                f"/api/v1/sessions/{session_id}/history",
                json={
                    "turn_ids": [turn_id],
                    "include": ["user", "assistant_text", "metadata"],
                },
            )
            (state.artifacts / "history.json").write_text(
                response.text, encoding="utf-8"
            )
            assert response.status_code == 200, response.text
            assert response.headers["X-Request-ID"]
            page = response.json()["data"]
            (turn,) = page["items"]
            # DTO 的 source_message_ids 只标识用户输入，不是 assembly selection。
            # TODO: API 尚无 assembly selector/selection/loss，补齐后再验完整 6.5。
            assert turn["source_message_ids"] == [
                message.id for message in expected if isinstance(message, HumanMessage)
            ]
            assert turn["user_messages"][0]["content"] == "HTTP canonical user"
            assert turn["assistant_text"] == [
                message.content
                for message in expected
                if isinstance(message, AIMessage)
            ]
            assert turn["assistant_text"] == ["HTTP canonical answer"]
            assert (
                "HTTP overlay" not in response.text
                and "HTTP restart prompt" not in response.text
            )
        finally:
            state.release_response.set()
            await asyncio.wait_for(dispatch, timeout=30)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_field", ["session_id", "assembly_id", "detail_id"])
async def test_restart_rejects_corrupt_detail_owner_before_native_http(
    dispatch_saver, native_http_server, owner_field
) -> None:
    saver, session_id, turn_id, sessions = dispatch_saver
    state, endpoint = native_http_server
    await invoke_native_dispatch(saver, session_id, turn_id, endpoint, state.api_key)
    (snapshot,) = saver.list_context_assemblies(session_id)
    source = next(
        entry for entry in snapshot.selection if entry.base_delta_role == "base"
    )
    from app.core.path_utils import get_session_path_resolver

    session = get_session_path_resolver(sessions).resolve_session_node(session_id)
    path = session / detail_relative_path(source.detail_ref)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["detail_ref"][owner_field] = f"wrong-{owner_field}"
    path.write_bytes(canonical_json_bytes(payload))
    with (
        RolloutCheckpointSaver(sessions) as restarted,
        pytest.raises(
            (RuntimeError, ValueError), match="source-mismatch.*typed identity"
        ),
    ):
        await invoke_native_dispatch(
            restarted, session_id, turn_id, endpoint, state.api_key
        )
    assert len(state.requests) == 1
    assert read_sealed_native_projection() is None
