"""生成器使用受控 Provider 替身，但真实写入 canonical SessionThread 与 admission。"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from app.core.execution_admission_context import execution_admission_scope
from app.core.path_utils import get_session_path_resolver
from app.core.session_thread_context import session_thread_owner_scope
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.assembly.content_store import (
    load_snapshot,
)
from scripts import generate_real_model_rollout_fixture as generator
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.itemized_dispatch_helpers import (
    native_http_server as native_http_server,  # noqa: PLC0414 - 使用当前测试文件的真实 HTTP 协议桩
)
from tests.integration.backend.sessions.rollout_context.test_context_plan_registry import (
    registry_db as registry_db,  # noqa: PLC0414 - 复用当前测试目录隔离的真实 Saver
)
from tests.support.catalog_session_bundle import seed_catalog_session_bundle
from tests.support.workspaces import prepare_default_test_workspace


def test_provider_fallback_preserves_each_original_failure() -> None:
    failures = {"provider-a": TypeError("首个模型调用失败"), "provider-b": ValueError("后备模型失败")}

    def invoke(provider_id, _messages, _use_tools):
        raise failures[provider_id]

    with pytest.raises(RuntimeError, match="fallback 全部失败") as captured:
        generator._invoke_with_provider_fallback(
            invoke, tuple(failures), "provider-a", [], provider_available={}, use_tools=False,
        )
    assert isinstance(captured.value.__cause__, ExceptionGroup)
    assert captured.value.__cause__.exceptions == tuple(failures.values())


def test_frozen_history_consumes_pending_dispatch_before_registering_model_call(
    registry_db, frozen_provider_prepare, monkeypatch: pytest.MonkeyPatch,
) -> None:
    saver, session, connection, accepted = registry_db
    thread = saver._storage.storage.main_thread_id(session)
    admission = saver.execution_admission(
        session, thread_id=thread, execution_id=accepted["initial_execution_id"],
    )
    register = saver.register_model_call
    registered = []

    def register_after_dispatch(*args, **kwargs):
        assert saver._prepared_dispatches == {}
        result = register(*args, **kwargs)
        registered.append(result)
        return result

    monkeypatch.setattr(saver, "register_model_call", register_after_dispatch)
    with (
        session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread),
        execution_admission_scope(admission, langgraph_thread_id=thread),
    ):
        initial = frozen_provider_prepare(saver, key="fixture-dispatch")
        retry = frozen_provider_prepare(saver, key="fixture-dispatch")
    assert initial == retry
    assert len(registered) == 2
    assert connection.execute("SELECT COUNT(*) FROM model_calls").fetchone() == (1,)
    assert connection.execute("SELECT COUNT(*) FROM context_assemblies").fetchone() == (1,)


@pytest.fixture
def real_generator_case(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, native_http_server,
):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    workspace = prepare_default_test_workspace(
        workspace_root=context.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
    )
    bundle = seed_catalog_session_bundle(
        workspace / ".boxteam" / "sessions", generator.SESSION_ID
    )
    http_state, endpoint = native_http_server
    providers = [
        {
            "id": identity, "model": "openai/gpt-4.1",
            "endpoint": endpoint,
            "api_key": http_state.api_key, "custom_llm_provider": "openai",
            "api_mode": {"protocol": protocol},
        }
        for identity, protocol in zip(("provider-a", "provider-b"), request.param[:2], strict=True)
    ]

    class FixtureConfig:
        def __init__(self, **_kwargs):
            pass

        def validate_workspace_config(self):
            pass

        def get_agent_runtime_config(self, _agent_id, **_kwargs):
            return {"providers": providers}

    monkeypatch.setattr(generator, "ConfigService", FixtureConfig)
    config_paths = (
        Path.cwd() / "configs" / "workspace_dev.jsonc",
        workspace / ".boxteam" / "workspace_real_model_override.jsonc",
    )
    environment_names = generator._configured_environment_names(config_paths)
    assert environment_names
    provider_environment = {
        name: f"fixture-{name.lower()}" for name in environment_names
    }
    for name in environment_names:
        monkeypatch.delenv(name, raising=False)

    class RunningBackendProcess:
        def __init__(self) -> None:
            self.pid = 4321
            self.info = {
                "cmdline": ["python", "-m", "app.main"],
            }

        def cwd(self) -> str:
            return str(generator.PROJECT_ROOT)

        def environ(self) -> dict[str, str]:
            return provider_environment

    monkeypatch.setattr(
        generator.psutil,
        "process_iter",
        lambda **_kwargs: [RunningBackendProcess()],
    )
    tool_every = request.param[2]
    if tool_every:
        http_state.output_tool_name = "inspect_fixture"
    return workspace, bundle, http_state, tool_every, provider_environment


@pytest.mark.parametrize("real_generator_case", [
    pytest.param(("chat_completions", "chat_completions", 0), id="chat-switch"),
    pytest.param(("responses", "responses", 0), id="responses-switch"),
    pytest.param(("responses", "chat_completions", 0), id="protocol-switch"),
    pytest.param(("responses", "responses", 1), id="tool-loop"),
], indirect=True)
def test_provider_stub_generator_writes_real_canonical_owner_and_admissions(
    real_generator_case,
) -> None:
    workspace, bundle, http_state, tool_every, provider_environment = real_generator_case
    generator.generate(workspace, end_turn=2, tool_every=tool_every, strict_provider_sequence=True)
    assert all(
        os.environ[name] == value
        for name, value in provider_environment.items()
    )
    expected_calls = 3 if tool_every else 2
    manifest = json.loads((bundle.directory / "session.json").read_text(encoding="utf-8"))
    assert manifest["workspace_id"] == bundle.node.workspace_id
    assert set(manifest) == {"session_id", "workspace_id", "created_at"}
    resolver = get_session_path_resolver(workspace / ".boxteam" / "sessions")
    assert resolver.catalog_store.get_node(bundle.session_id).current_provider_id == "provider-b"
    rollout = resolver.resolve_thread_node(bundle.session_id, bundle.main_thread_id) / "rollout"
    assert rollout.is_dir()
    with sqlite3.connect(rollout / "index.sqlite") as connection:
        assert connection.execute(
            "SELECT DISTINCT session_id, thread_id FROM executions"
        ).fetchall() == [(bundle.session_id, bundle.main_thread_id)]
        assert connection.execute(
            "SELECT COUNT(*) FROM turn_records WHERE status = 'completed'"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM turn_acceptances"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM executions WHERE outcome='completed'"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT COUNT(*) FROM model_calls WHERE dispatch_state='completed' AND outcome='completed'"
        ).fetchone() == (expected_calls,)
        sealed_requests = [
            load_snapshot(connection, row[0])["request_hash_preimage"]
            for row in connection.execute("SELECT snapshot_json FROM context_assemblies ORDER BY created_at")
        ]
    assert sealed_requests == [request["body"] for request in http_state.requests]
    assert len(sealed_requests) == expected_calls
    if tool_every:
        tool_results = [item for item in sealed_requests[1]["input"] if item.get("type") == "function_call_output"]
        assert len(tool_results) == 1
        assert "inspect_fixture 工具调用参数校验失败" in tool_results[0]["output"]

    backup = bundle.directory / ".rollout-backup-fixture"
    shutil.copytree(rollout, backup)
    (rollout / "failed-generation").write_text("失败产物", encoding="utf-8")
    generator._restore_generation_backup(workspace)
    assert not backup.exists()
    assert not (rollout / "failed-generation").exists()
    assert (rollout / "index.sqlite").is_file()
    assert not (bundle.directory / "rollout").exists()
    assert not (rollout.parent / ".rollout-failed-regeneration").exists()
