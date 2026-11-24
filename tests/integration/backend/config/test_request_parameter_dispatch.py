"""Agent／调用参数经真实 Saver 封存和 SDK 序列化，不替代完整产品验收。"""

from __future__ import annotations

import asyncio

import pytest
from langchain.agents.middleware import ModelRequest
from langgraph.runtime import ExecutionInfo, Runtime

from app.core.checkpoint_config import build_checkpoint_config
from app.domain.itemized.hash.request_hash import context_request_hash
from app.domain.itemized.serialization import normalize_wire_request
from app.runtime.network_proxy import ProxyRoutes
from app.runtime.provider_model import build_model_from_provider
from configs.network import DirectProxy
from tests.integration.backend.sessions.itemized_dispatch_helpers import (
    create_dispatch_agent,
    dispatch_execution_scope,
    dispatch_saver,
    native_dispatch_workspace,
    native_http_server,
)
from tests.integration.backend.sessions.test_itemized_dispatch_contract import (
    dispatch_bridge,
)

__all__ = ["dispatch_bridge", "dispatch_saver", "native_dispatch_workspace", "native_http_server"]


@pytest.mark.parametrize("protocol", ["chat_completions", "responses"])
@pytest.mark.parametrize("mode", ["sync", "async"])
async def test_temporary_parameter_nodes_match_sealed_hash_and_actual_sdk_body(
    dispatch_saver, native_http_server, dispatch_bridge, protocol: str, mode: str,
) -> None:
    saver, session_id, turn_id, _ = dispatch_saver
    state, endpoint = native_http_server
    thread_id = saver._storage.storage.main_thread_id(session_id)
    checkpoint = saver.get_tuple(build_checkpoint_config(thread_id))
    model = build_model_from_provider(provider={
        "provider_id": "parameter-test", "model_id": "main", "model": "gpt-4.1",
        "endpoint": endpoint, "custom_llm_provider": "openai", "api_key": state.api_key,
        "api_mode": {"protocol": protocol, "streaming": True,
                     "model_info": {}, "supports_reasoning": {}},
        "request_options": {"parameters": {
            "temperature": 1, "top_p": 0.9, "max_tokens": 128,
            "extra_body": {"parent_only": True},
        }},
        "proxy_routes": ProxyRoutes.from_configuration(DirectProxy()),
    }, runtime_config={"temperature": 0, "top_p": None, "max_output_tokens": None})
    execution = dispatch_bridge(
        session_id=session_id, instruction="验证参数继承与封存", checkpointer=saver,
    )
    create_dispatch_agent(model, execution=execution, tools=[], checkpointer=saver)
    request = ModelRequest(
        model=model, messages=[], tools=[],
        model_settings={"max_tokens": 32, "extra_body": {
            "vendor_switch": False, "vendor_nullable": None,
        }},
        runtime=Runtime(execution_info=ExecutionInfo(
            checkpoint_id=checkpoint.checkpoint["id"], checkpoint_ns="", thread_id=thread_id,
            task_id=f"parameter-inheritance:{protocol}:{mode}",
        )),
    )
    try:
        for parameters, error in (
            ({"extra_body": {"temperature": 0}}, "extra_body"),
            ({"max_tokens": 16, "max_output_tokens": 16}, "别名重复"),
        ):
            with dispatch_execution_scope(saver, session_id, turn_id), pytest.raises(ValueError, match=error):
                invalid = request.override(model_settings=parameters)
                if mode == "sync":
                    await asyncio.to_thread(execution.execute, invalid)
                else:
                    await execution.aexecute(invalid)
            assert state.requests == []
            assert saver.list_context_assemblies(session_id, thread_id) == ()
        with dispatch_execution_scope(saver, session_id, turn_id):
            if mode == "sync":
                await asyncio.to_thread(execution.execute, request)
            else:
                await execution.aexecute(request)
        assert len(state.requests) == 1
        body = state.requests[0]["body"]
        output_key = "max_output_tokens" if protocol == "responses" else "max_tokens"
        assert body[output_key] == 32
        assert body["temperature"] == 0
        assert body["vendor_switch"] is False and body["vendor_nullable"] is None
        assert {"top_p", "parent_only", "extra_body",
                "max_tokens" if protocol == "responses" else "max_output_tokens"}.isdisjoint(body)
        assert body["stream"] is True
        if protocol == "chat_completions":
            assert body["stream_options"] == {"include_usage": True}
        else:
            assert "stream_options" not in body
        snapshot, = saver.list_context_assemblies(session_id, thread_id)
        snapshot.validate_hashes()
        assert snapshot.request_hash_preimage == normalize_wire_request(body)
        assert snapshot.request_hash == context_request_hash(
            snapshot.as_sealed_plan(), snapshot.provider_version,
            projector_id=snapshot.projector_id, projector_version=snapshot.projector_version,
            target_format=protocol, wire_request=normalize_wire_request(body),
        )
        with pytest.raises(ValueError, match="source-mismatch.*sealed assembly binding"):
            await model.ainvoke("缺少封存的 Agent 调用")
        assert len(state.requests) == 1
    finally:
        await model.aclose()
