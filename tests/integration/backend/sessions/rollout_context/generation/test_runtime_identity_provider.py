"""真实 factory/Saver/Provider SDK 请求边界；HTTP 传输是显式集成桩。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.agents.providers import wire_finalize
from app.agents.sealed_assembly_dispatch import sealed_assembly_dispatch_scope
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.core.trace_middleware import reset_current_gateway_id, set_current_gateway_id
from app.domain.itemized.hashing import sha256_jcs
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.rollout_context.test_agent_production_context import (
    factory_context as factory_context,  # noqa: PLC0414 - 沿正式 factory 入口复用 fixture
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - 工作区按本测试路径隔离
)
from tests.integration.backend.sessions.rollout_test_workspace import (
    prepare_rollout_workspace,
)


class _HTTPBoundaryReached(BaseException):
    """已取得 SDK 序列化后的请求；禁止真的发往外部 Provider。"""


@pytest.fixture(autouse=True)
def runtime_identity_workspace(request: pytest.FixtureRequest) -> None:
    prepare_rollout_workspace(request)


@pytest.fixture
def runtime_gateway_id() -> str:
    return "gateway_0123456789abcdef0123456789abcdef"


@pytest.fixture
def outgoing_http(monkeypatch, request):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    requests = []

    def send(_client, actual, **_kwargs):
        assert actual.url.path in {"/v1/chat/completions", "/v1/responses"}
        assert actual.headers["authorization"] == "Bearer factory-fixture-key"
        body = json.loads(actual.content)
        requests.append(body)
        name = request.node.name.replace("[", "-").replace("]", "")
        artifact = context.artifacts_dir / f"{name}-{len(requests)}.json"
        artifact.write_bytes(actual.content)
        raise _HTTPBoundaryReached()

    monkeypatch.setattr(httpx.Client, "send", send)
    return requests


def _provider_handler(actual):
    next(actual.model.bind_tools(actual.tools).stream(actual.messages))
    raise AssertionError("Provider HTTP 边界未被调用")


def _restore_and_send(saver, session, thread, model):
    snapshot = saver._storage.assemblies.list_context_assemblies(session, thread)[-1]
    cold = RolloutCheckpointSaver(saver._storage.storage.sessions_dir)
    restored = cold.get_context_assembly(session, thread, assembly_id=snapshot.assembly_id)
    assert restored == snapshot
    assert restored.model_call_id is not None
    reference = cold.issue_context_dispatch_ref(
        restored, attempt_key=restored.plan_id, callback_model_call_id=restored.model_call_id,
    )
    payload = cold.read_context_for_dispatch(reference)
    messages, tools, losses = payload["messages"], payload["tools"], payload["losses"]
    assert losses == ()
    native = payload.get("native_projection")
    admission = cold.execution_admission(session, thread_id=thread, execution_id=restored.execution_id)
    with (
        session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
        execution_admission_scope(admission, langgraph_thread_id=thread),
        sealed_assembly_dispatch_scope(reference, restored, resource_refs=payload["resource_refs"], native_projection=native),
    ):
        next(model.bind_tools(tools).stream(messages))


@pytest.mark.parametrize("factory_context", ["chat_completions", "responses"], indirect=True)
def test_runtime_scope_reaches_serialized_http_and_restores_without_host_root(factory_context, outgoing_http, runtime_gateway_id):
    saver, session, thread, _connection, _selection, _manager, captured, tools, dispatch, files = factory_context
    with pytest.raises(_HTTPBoundaryReached):
        dispatch("r02-native", provider_handler=_provider_handler)
    with pytest.raises(_HTTPBoundaryReached):
        _restore_and_send(saver, session, thread, captured["model"])
    assert len(outgoing_http) == 2
    assert outgoing_http[0] == outgoing_http[1]
    serialized = json.dumps(outgoing_http, ensure_ascii=False)
    assert str(files.workspace_root) not in serialized
    assert "cwd）：`.`" in serialized
    runtime = SimpleNamespace(tool_call_id="call-r02-files", state={"files": {}}, store=None)
    # 文件入口显式传递 Gateway 身份；不能让它改变上方 Agent 构造的来源范围。
    token = set_current_gateway_id(runtime_gateway_id)
    try:
        result = tools["write_file"].invoke({
            "file_path": "r02-proof.txt", "content": "R02 工作区文件工具证据\n",
            "expected_version": "absent", "runtime": runtime,
        })
        assert result.status == "success", result.content
        assert (files.workspace_root / "r02-proof.txt").read_text() == "R02 工作区文件工具证据\n"
        read = tools["read_file"].invoke({"path": "r02-proof.txt", "runtime": runtime})
        assert read.status == "success", read.content
        assert "R02 工作区文件工具证据" in read.content
        assert str(files.workspace_root) not in str(read.content)
    finally:
        reset_current_gateway_id(token)


@pytest.mark.parametrize("factory_context", ["chat_completions", "responses"], indirect=True)
def test_restored_legacy_r02_is_rejected_without_rewriting_or_http(factory_context, outgoing_http, monkeypatch):
    saver, session, thread, _connection, _selection, manager, captured, _tools, dispatch, files = factory_context
    source = next(source for source in manager.prepare_sources().sources if source.descriptor.source_id == "instruction:runtime-identity")
    legacy = source.content.replace("cwd）：`.`", f"cwd）：`{files.workspace_root}`")
    assert str(files.workspace_root) in legacy
    manager.observe("instruction:runtime-identity", legacy, revision=sha256_jcs({"legacy-r02": legacy}))
    # 显式复现旧 writer 的已封存记录；只在建历史数据时关闭新增门禁。
    with monkeypatch.context() as old_writer:
        old_writer.setattr(wire_finalize, "validate_provider_system_scope", lambda _payload: None)
        with pytest.raises(_HTTPBoundaryReached):
            dispatch("r02-legacy", provider_handler=_provider_handler)
    before = saver._storage.assemblies.list_context_assemblies(session, thread)
    with pytest.raises(ValueError, match="real-path-not-allowed: R02"):
        _restore_and_send(saver, session, thread, captured["model"])
    assert len(outgoing_http) == 1
    assert saver._storage.assemblies.list_context_assemblies(session, thread) == before
    assert str(files.workspace_root) in json.dumps(before[-1].request_hash_preimage, ensure_ascii=False)
