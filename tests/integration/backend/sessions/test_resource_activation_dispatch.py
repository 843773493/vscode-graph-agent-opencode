"""真实 Saver/Bridge/Provider HTTP 的混合资源边界与冷恢复合同。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.agents.providers.litellm_chat import BoxteamLiteLLMChatModel
from app.agents.providers.openai_responses import BoxteamOpenAIResponsesModel
from app.agents.sealed_assembly_dispatch import sealed_assembly_dispatch_scope
from app.core.identifier import create_uuid_hex
from app.domain.itemized.resource_activation import ResourceActivationPolicySnapshot
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.integration.backend.sessions.itemized_dispatch_helpers import (
    dispatch_execution_scope,
    inspection_resource_registry,
    invoke_provider_dispatch,
    native_dispatch_workspace,
    native_http_server,
    seed_dispatch_history,
)

__all__ = ["inspection_resource_registry", "native_dispatch_workspace", "native_http_server"]


@pytest.fixture
def activation_case(native_dispatch_workspace, session_bundle_factory, inspection_resource_registry):
    sessions = native_dispatch_workspace / ".boxteam/sessions"
    session_id = f"ses_{create_uuid_hex()}"
    session_bundle_factory(sessions, session_id)
    policy = ResourceActivationPolicySnapshot(
        default_boundary="turn", boundaries={"mcp_tool_catalog": "model_call"},
    )
    with RolloutCheckpointSaver(sessions) as saver:
        turn_id = seed_dispatch_history(
            saver, session_id, activation_policy=policy, registry=inspection_resource_registry,
        )
        yield saver, session_id, turn_id, sessions, inspection_resource_registry, policy


def _load_activation(saver, snapshot):
    return saver.load_sealed_resource_activation(
        snapshot.session_id, thread_id=snapshot.thread_id,
        assembly_id=snapshot.assembly_id, plan_hash=snapshot.plan_hash,
        request_hash=snapshot.request_hash,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("target_format", ["responses", "chat_completions"])
async def test_mixed_activation_survives_lost_execution_and_cold_resume(
    activation_case, native_http_server, target_format, monkeypatch,
):
    """真实 execution 丢失后恢复同 Turn；policy/Turn binding 不漂移，新 call 更新 MCP。"""
    saver, session, turn, sessions, registry, policy = activation_case
    state, endpoint = native_http_server
    prepared = []
    prepare = saver.prepare_context_for_provider

    def record_prepare(*args, **kwargs):
        result = prepare(*args, **kwargs)
        prepared.append(result)
        return result

    monkeypatch.setattr(saver, "prepare_context_for_provider", record_prepare)
    await invoke_provider_dispatch(
        saver, session, turn, endpoint, state.api_key,
        target_format=target_format, registry=registry, activation_policy=policy,
    )
    thread = saver._storage.storage.main_thread_id(session)
    (first,) = saver.list_context_assemblies(session, thread)
    original = _load_activation(saver, first)
    original_bindings = {binding.resource_kind: binding for binding in original.bindings}
    assert set(original_bindings) == {"skills", "mcp_tool_catalog"}
    assert original_bindings["skills"].effective_boundary == "turn"
    assert original_bindings["mcp_tool_catalog"].effective_boundary == "model_call"
    assert original.parent is not None
    assert original.activation_policy == policy
    lost = saver.mark_execution_lost(
        session, thread_id=thread, turn_id=turn, execution_id=first.execution_id,
    )
    assert lost["outcome"] == "execution_lost"
    canonical_before = saver.read_canonical_items(session)
    for published in registry.snapshots():
        registry.publish(replace(
            published, revision="activation-revision-2",
            payload={"instruction": f"最新 {published.resource_kind} 正文"}, generation=8,
        ))
    hot_policy = ResourceActivationPolicySnapshot(default_boundary="model_call", boundaries={})
    assert hot_policy.hash != policy.hash

    # 重建唯一 Saver，恢复事实必须来自 durable owner，而非旧 coordinator 的内存。
    with RolloutCheckpointSaver(sessions) as restarted:
        resumed = restarted.resume_turn(session, thread_id=thread, turn_id=turn, job_id="activation-resume")
        assert resumed["execution_id"] != first.execution_id
        assert _load_activation(restarted, first) == original
        prepare = restarted.prepare_context_for_provider
        monkeypatch.setattr(restarted, "prepare_context_for_provider", record_prepare)
        await invoke_provider_dispatch(
            restarted, session, turn, endpoint, state.api_key,
            target_format=target_format, registry=registry, activation_policy=hot_policy,
        )
        second = next(snapshot for snapshot in restarted.list_context_assemblies(session, thread)
                      if snapshot.assembly_id != first.assembly_id)
        recovered = _load_activation(restarted, second)
        bindings = {binding.resource_kind: binding for binding in recovered.bindings}
        assert recovered.execution_id == resumed["execution_id"]
        assert recovered.turn_id == original.turn_id == turn
        assert recovered.activation_policy == policy
        assert recovered.activation_policy_hash == original.activation_policy_hash
        assert recovered.activation_policy_revision == original.activation_policy_revision
        assert bindings["skills"] == original_bindings["skills"]
        assert bindings["mcp_tool_catalog"].effective_boundary == "model_call"
        assert bindings["mcp_tool_catalog"].revision == "activation-revision-2"
        assert bindings["mcp_tool_catalog"].content_hash != original_bindings["mcp_tool_catalog"].content_hash
        assert recovered.parent is not None and recovered.parent.execution_id == resumed["execution_id"]
        assert recovered.parent.bindings == original.parent.bindings
        assert recovered.parent.activation_policy == original.parent.activation_policy
        assert restarted.read_canonical_items(session)[:len(canonical_before)] == canonical_before
        assert _load_activation(restarted, first) == original
        assert len(state.requests) == 2

    # 原请求冷读取后直接经过 Provider 发送；禁止重准备或读取当前 Registry。
    def unexpected(*_args, **_kwargs):
        raise AssertionError("已封存请求重试不得重新冻结或读取当前 Registry")

    with RolloutCheckpointSaver(sessions) as cold:
        reference = prepared[-1]["dispatch_ref"]
        with monkeypatch.context() as patch:
            patch.setattr(registry, "capture", unexpected)
            patch.setattr(cold, "prepare_context_for_provider", unexpected)
            payload = cold.read_context_for_dispatch(reference)
            assert payload["snapshot"] == second
            assert _load_activation(cold, first) == original
            with (
                dispatch_execution_scope(cold, session, turn, activation_policy=hot_policy, registry=registry),
                sealed_assembly_dispatch_scope(
                    reference, payload["snapshot"], resource_refs=payload["resource_refs"],
                    native_projection=payload.get("native_projection"),
                ),
            ):
                model_type = {
                    "responses": BoxteamOpenAIResponsesModel,
                    "chat_completions": BoxteamLiteLLMChatModel,
                }[target_format]
                model = model_type(
                    model="openai/gpt-4.1", api_key=state.api_key, api_base=endpoint,
                    custom_llm_provider="openai", streaming=True, max_retries=0,
                )
                bound = model.bind_tools(payload["tools"]) if payload["tools"] else model
                await bound.ainvoke(payload["messages"])
        assert len(cold.list_context_assemblies(session, thread)) == 2
    assert len(state.requests) == 3
    assert state.requests[1]["body"] == state.requests[2]["body"]
