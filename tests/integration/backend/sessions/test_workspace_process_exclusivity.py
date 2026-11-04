"""真实 Backend 的工作区独占跟随物理根，不跟随 Gateway home。"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import cast

import httpx
import pytest

from app.testing.model_stream import build_cassette, chat_completion_frames
from configs.runtime import read_jsonc_object
from tests.harness.python.run_context import TestRunContext
from tests.support.model_stream.config import ModelStreamReplay, model_stream_replay
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS
from tests.support.processes.workspace import (
    BackendProcess,
    close_backend_process,
    start_backend_process,
)


@pytest.fixture
def process_replay(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
) -> Iterator[ModelStreamReplay]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_port_block_for_file(Path(request.node.path))
    config_path = Path(integration_workspace_root_path) / ".boxteam/workspace.jsonc"
    config = read_jsonc_object(config_path)
    providers = cast(list[dict[str, object]], cast(dict[str, object], config["llm"])["providers"])
    for provider in providers:
        provider.update({
            "endpoint": f"http://127.0.0.1:{ports.port(80)}/v1",
            "model": "e2e-stub-model", "api_key": "e2e-local-model-key", "custom_llm_provider": "openai",
            "api_mode": {
                "protocol": "chat_completions", "model_info": {"supports_function_calling": True, "supports_reasoning": False},
                "supports_reasoning": {"reasoning_content": False},
            },
        })
        provider.pop("auth", None)
        provider.pop("request_options", None)
    for agent in cast(dict[str, dict[str, object]], config["agents"]).values():
        cast(dict[str, object], agent["model"])["fallback_providers"] = []
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with model_stream_replay(
        context.artifacts_dir, runtime_root=context.runtime_root_for_node(request.node.nodeid),
        business_assertion="workspace-lock-cross-home-takeover-keeps-executed-history",
    ) as replay:
        yield replay


@pytest.fixture
def backend_lifecycle(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
    process_replay: ModelStreamReplay,
) -> Callable[[str, int], AbstractContextManager[BackendProcess]]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_port_block_for_file(Path(request.node.path))

    @contextmanager
    def start(name: str, offset: int) -> Iterator[BackendProcess]:
        backend = start_backend_process(
            workspace_root=integration_workspace_root_path, port=ports.port(offset),
            log_name=name, boxteam_home=context.output_root / f"home-{name}",
            artifacts_dir=context.artifacts_dir,
            asgi_app="tests.support.e2e_control.backend_app:app",
            env_overrides={**process_replay.environment, "LITELLM_LOCAL_MODEL_COST_MAP": "true"},
        )
        try:
            yield backend
        finally:
            close_backend_process(backend)

    return start


def _data(response: httpx.Response) -> dict:
    assert response.status_code == 200, response.text
    payload = response.json()
    assert response.headers["X-Request-ID"] == payload["request_id"]
    return payload["data"]


def _complete_turn(
    client: httpx.Client, replay: ModelStreamReplay, model_url: str,
    session_id: str, thread_id: str, message: str, answer: str,
) -> tuple[str, dict]:
    cassette = build_cassette(
        asset_id="workspace-process-exclusivity", model="e2e-stub-model", url=model_url,
        frames=chat_completion_frames(
            response_id="chatcmpl-process-exclusivity", model="e2e-stub-model",
            deltas=({"role": "assistant", "content": answer},), finish_reason="stop",
        ),
    )
    interaction, = cast(list[dict[str, object]], cassette.raw["interactions"])
    call, = replay.control.plan_model_calls(session_id, thread_id, [interaction])
    accepted = _data(client.post(f"/api/v1/sessions/{session_id}/messages", json={
        "message": {"content": message}, "run": {"mode": "single_agent", "agent_id": "default"},
    }))
    deadline = time.monotonic() + 60
    while True:
        job = _data(client.get(f"/api/v1/jobs/{accepted['job_id']}"))
        if job["status"] in {"completed", "failed", "cancelled", "interrupted"}:
            assert job["status"] == "completed", job
            assert (job["session_id"], job["thread_id"]) == (session_id, thread_id)
            break
        assert time.monotonic() < deadline, job
        time.sleep(0.1)
    artifact = json.loads((replay.artifacts / f"{call}.json").read_text(encoding="utf-8"))
    assert artifact["identity"] == {"session_id": session_id, "thread_id": thread_id, "model_call_id": call}
    return call, artifact["request"]


def test_another_home_cannot_bypass_workspace_lock_and_can_take_over_after_exit(
    backend_lifecycle: Callable[[str, int], AbstractContextManager[BackendProcess]],
    request: pytest.FixtureRequest, process_replay: ModelStreamReplay,
) -> None:
    context = TestRunContext.from_test_file(Path(request.node.path))
    model_url = f"http://127.0.0.1:{integration_port_block_for_file(Path(request.node.path)).port(80)}/v1/chat/completions"
    with backend_lifecycle("original", 0) as original:
        headers = {**LOCAL_TOKEN_HEADERS, "X-BoxTeam-Workspace-Id": original.workspace_id}
        with httpx.Client(base_url=f"http://127.0.0.1:{original.port}", headers=headers, timeout=20) as client:
            created = client.post("/api/v1/sessions", json={"title": "跨 home 保留的会话"})
            assert created.status_code == 200, created.text
            session = created.json()["data"]
            session_id = session["session_id"]
            main_thread = client.get(f"/api/v1/sessions/{session_id}/main-thread")
            assert main_thread.status_code == 200, main_thread.text
            thread = main_thread.json()["data"]
            first_call, first_wire = _complete_turn(
                client, process_replay, model_url, session_id, thread["thread_id"],
                "记住原工作区的执行历史", "原工作区历史已记录",
            )
            session = _data(client.get(f"/api/v1/sessions/{session_id}"))
            history = _data(client.post(f"/api/v1/sessions/{session_id}/history", json={"direction": "tail"}))
            assert history["items"][-1]["final_response"] == "原工作区历史已记录"
            identity_path = Path(original.workspace_root) / ".boxteam/workspace-identity.json"
            identity_bytes = identity_path.read_bytes()
            assert json.loads(identity_bytes)["workspace_id"] == original.workspace_id

            with (
                pytest.raises(RuntimeError, match=r"后端进程提前退出:.*returncode=[1-9]"),
                backend_lifecycle("contender", 1),
            ):
                pytest.fail("不同 home 的第二个 Backend 不应进入可服务状态")
            rejected_log = (context.artifacts_dir / "contender.stderr.log").read_text(encoding="utf-8")
            assert "SQLite 状态库已被另一个进程占用" in rejected_log
            assert ".workspace.sqlite.lock" in rejected_log
            assert original.process.poll() is None
            health = client.get("/api/v1/health")
            assert health.status_code == 200, health.text
            assert health.json()["workspace_id"] == original.workspace_id
            assert identity_path.read_bytes() == identity_bytes

    with backend_lifecycle("successor", 2) as successor:
        assert successor.workspace_id == original.workspace_id
        assert identity_path.read_bytes() == identity_bytes
        with httpx.Client(base_url=f"http://127.0.0.1:{successor.port}", headers=headers, timeout=20) as client:
            restored = client.get(f"/api/v1/sessions/{session_id}")
            assert restored.status_code == 200, restored.text
            assert restored.json()["data"] == session
            restored_thread = client.get(f"/api/v1/sessions/{session_id}/main-thread")
            assert restored_thread.status_code == 200, restored_thread.text
            assert restored_thread.json()["data"] == thread
            assert restored.headers["X-Request-ID"] == restored.json()["request_id"]
            restored_history = _data(client.post(f"/api/v1/sessions/{session_id}/history", json={"direction": "tail"}))
            assert restored_history["items"] == history["items"]
            next_call, next_wire = _complete_turn(
                client, process_replay, model_url, session_id, thread["thread_id"],
                "换 home 后继续原会话", "已从原检查点继续",
            )
            prefix = first_wire["messages"]
            assert next_wire["messages"][:len(prefix)] == prefix
            assert next_wire["messages"][len(prefix):] == [
                {"role": "assistant", "content": [{"text": "原工作区历史已记录", "type": "text"}]},
                {"role": "user", "content": "换 home 后继续原会话"},
            ]
            continued = _data(client.post(f"/api/v1/sessions/{session_id}/history", json={"direction": "tail"}))
            assert continued["items"][-1]["final_response"] == "已从原检查点继续"
            assert {path.stem for path in process_replay.artifacts.glob("*.json")} == {first_call, next_call}
        (context.artifacts_dir / "ownership.json").write_text(json.dumps({
            "workspace_id": successor.workspace_id, "session": session, "main_thread": thread,
            "original_pid": original.process.pid, "successor_pid": successor.process.pid,
            "original_exit_code": original.process.returncode,
            "history": history, "restored_history": restored_history, "continued": continued,
            "model_calls": [first_call, next_call],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
