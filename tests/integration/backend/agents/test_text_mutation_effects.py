"""真实文件 owner 与两跳联邦在退出、丢回执和取消时的效果知识边界。"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.resource_uri import resource_display_uri
from app.core.trace_middleware import (
    reset_current_gateway_id,
    reset_current_request_id,
    set_current_gateway_id,
    set_current_request_id,
)
from app.schemas.internal_v2.text_mutation import (
    PatchText,
    TextMutationRequest,
)
from app.services.infrastructure.gateway_client_common import GatewayTransportConnection
from app.services.infrastructure.workspace_files import text_owner
from app.services.infrastructure.workspace_files.gateway_text_mutation import (
    GatewayTextMutation,
)
from app.services.infrastructure.workspace_files.paths import WorkspaceFilePaths
from app.services.infrastructure.workspace_files.text_journal import (
    load_text_mutation_snapshots,
)
from app.services.infrastructure.workspace_files.text_owner import (
    WorkspaceTextMutationOwner,
)
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    _prepare_workspace,
    _tool,
)
from tests.support.api_helpers import create_session
from tests.support.communication_federation import open_communication_triad
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.test_session_communication_pipeline import (
    _prepare_workspace,
    _tool,
)
from tests.support.api_helpers import create_session
from tests.support.communication_federation import open_communication_triad
from tests.support.model_stream_config import model_stream_replay
from tests.support.processes.gateway import LOCAL_TOKEN_HEADERS
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.workspace import close_backend_process


@pytest.fixture
def mutation_model(request):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    with model_stream_replay(
        context.artifacts_dir.parent, runtime_root=context.artifacts_dir.parent / "runtime" / request.node.name,
        business_assertion="text-mutation-effects",
    ) as replay:
        yield replay


@pytest.fixture
async def mutation_case(request, mutation_model):
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    artifacts = context.artifacts_dir / request.node.name / uuid4().hex
    artifacts.mkdir(parents=True, exist_ok=True)
    async with open_communication_triad(context, _prepare_workspace,
        backend_asgi_app="tests.integration.backend.agents.resource_text_mutation_fault_backend:app",
        backend_environment={**mutation_model.environment, "BOXTEAM_TEST_MUTATION_ARTIFACTS": str(artifacts)}) as nodes:
        source, target = nodes["b"], nodes["c"]
        gateway = GatewayTextMutation(workspace_id=source.workspace_id,
            connection=GatewayTransportConnection(gateway_url=str(source.client.base_url).rstrip("/")),
            local_token_supplier=lambda: LOCAL_TOKEN_HEADERS["X-Local-Token"])
        owner = WorkspaceTextMutationOwner(paths=WorkspaceFilePaths(workspace=source.workspace,
            workspace_id=source.workspace_id, bindings=None), gateway=gateway)
        gateway_token = set_current_gateway_id(source.gateway_id)
        request_token = set_current_request_id("req-file-effects")
        try:
            yield source, target, owner, artifacts, mutation_model
        finally:
            # 即使断言失败也释放本测试持有的阻塞点，不能把 worker 留给进程退出。
            for phase in ("cancel", "timeout"):
                (artifacts / (phase + "-release")).touch()
            reset_current_request_id(request_token)
            reset_current_gateway_id(gateway_token)


def _patch(source, target, phase: str) -> PatchText:
    names = [phase + "-" + ordinal + ".txt" for ordinal in ("first", "second", "third")]
    for name in names:
        (source.workspace / "src" / name).write_text("SOURCE DECOY", encoding="utf-8")
    root = resource_display_uri(scope="workspace", scope_id=target.workspace_id, kind="files",
        tail_segments=("root", "src"), gateway_authority=target.gateway_id)
    return PatchText(target=root, input="*** Begin Patch\n" + "".join(
        f"*** Add File: {name}\n+TARGET {name}\n" for name in names) + "*** End Patch",
        expected_versions=dict.fromkeys(names, "absent"), explanation="核实目标文件效果")


async def _wait(path: Path, seconds: float = 10) -> None:
    async with asyncio.timeout(seconds):
        while not path.is_file():
            await asyncio.sleep(0.02)


async def _read(source, uri):
    response = await source.client.post("/api/v1/resources/navigation", json={"uri": uri, "operation": "read"})
    assert response.status_code == 200, response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"]
    return response.json()["data"]["content"]


async def test_target_exit_and_lost_receipt_stay_unknown_until_reopened(mutation_case):
    source, target, owner, artifacts, _ = mutation_case
    command = _patch(source, target, "partial")
    partial = await owner.execute_async(command)
    assert partial["status"] == "error" and partial["effect"] == "partial" and partial["retryable"] is False
    assert [item["state"] for item in partial["files"]] == ["published", "not_published", "not_attempted"]
    assert await _read(source, command.target + "/partial-first.txt") == "TARGET partial-first.txt"
    for name in ("partial-second.txt", "partial-third.txt"):
        assert not (target.workspace / "src" / name).exists()
    for name in command.expected_versions:
        assert (source.workspace / "src" / name).read_text(encoding="utf-8") == "SOURCE DECOY"
    (artifacts / "partial-source.json").write_text(json.dumps(partial, ensure_ascii=False, indent=2), encoding="utf-8")
    for phase, exit_code, published in (("publish-exit", 72, 1), ("response-drop", 73, 3)):
        command = _patch(source, target, phase)
        receipt = await owner.execute_async(command)
        assert receipt["status"] == "error" and receipt["effect"] == "unknown" and receipt["retryable"] is False
        files = receipt["files"]
        assert [item["state"] for item in files] == ["unknown"] * 3
        assert {item["path"]: item["expected_version"] for item in files} == {
            command.target + "/" + name: "absent" for name in command.expected_versions}
        assert load_text_mutation_snapshots(json.dumps(receipt), workspace_root=source.workspace) == []
        assert str(source.workspace) not in json.dumps(receipt) and str(target.workspace) not in json.dumps(receipt)
        assert await asyncio.to_thread(target.backend.process.wait, timeout=10) == exit_code
        await asyncio.to_thread(close_backend_process, target.backend)
        for index, name in enumerate(command.expected_versions):
            path = target.workspace / "src" / name
            assert path.is_file() == (index < published)
            if path.exists():
                assert path.read_text(encoding="utf-8") == "TARGET " + name
            assert (source.workspace / "src" / name).read_text(encoding="utf-8") == "SOURCE DECOY"
        await asyncio.to_thread(target.restart_backend, "start")
        for name in list(command.expected_versions)[:published]:
            assert await _read(source, command.target + "/" + name) == "TARGET " + name
        if phase == "response-drop":
            target_receipt = json.loads((artifacts / "response-drop.json").read_text(encoding="utf-8"))["receipt"]
            assert target_receipt["effect"] == "completed"
            assert [item["state"] for item in target_receipt["files"]] == ["published"] * 3
        (artifacts / (phase + "-source.json")).write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")


async def test_cancelled_wait_and_http_timeout_do_not_claim_target_stopped(mutation_case, monkeypatch):
    source, target, owner, artifacts, _ = mutation_case
    command = _patch(source, target, "cancel")
    completed = threading.Event()
    observations = []
    update = text_owner.update_text_mutation_journal

    def observed_journal(journal_id, publications, **options):
        update(journal_id, publications, **options)
        if options.get("workspace_root") == source.workspace:
            observations.append((journal_id, publications))
            completed.set()

    monkeypatch.setattr(text_owner, "update_text_mutation_journal", observed_journal)
    task = asyncio.create_task(owner.execute_async(command))
    await _wait(artifacts / "cancel-entered.json")
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as cancelled:
        await task
    receipt = cancelled.value.args[0].model_dump()
    assert receipt["code"] == "cancellation_requested" and receipt["effect"] == "unknown"
    assert receipt["target"] == command.target and receipt["retryable"] is False
    assert [item["state"] for item in receipt["files"]] == ["unknown"] * 3
    assert all(item["expected_version"] == "absent" for item in receipt["files"])
    (artifacts / "cancel-source.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    assert not completed.is_set()
    assert (target.workspace / "src/cancel-first.txt").read_text(encoding="utf-8") == "TARGET cancel-first.txt"
    assert not (target.workspace / "src/cancel-second.txt").exists()
    (artifacts / "cancel-release").touch()
    assert await asyncio.to_thread(completed.wait, 10), "取消后原发布线程未完成原观察journal"
    journal_id, publications = observations.pop()
    assert [item["state"] for item in publications] == ["published"] * 3
    snapshots = load_text_mutation_snapshots(json.dumps({"journal_id": journal_id}), workspace_root=source.workspace)
    assert len(snapshots) == 3
    for name in command.expected_versions:
        assert await _read(source, command.target + "/" + name) == "TARGET " + name
        assert (source.workspace / "src" / name).read_text(encoding="utf-8") == "SOURCE DECOY"

    command = _patch(source, target, "timeout")
    local = command.model_copy(update={"target": command.target.replace(target.gateway_id + "/", "")})
    response = await target.client.post("/api/v1/resources/text-mutations", json=TextMutationRequest.from_command(
        local, timeout_ms=100).model_dump(mode="json", exclude_none=True))
    assert response.status_code == 200, response.text
    receipt = response.json()["data"]["receipt"]
    assert receipt["effect"] == "unknown" and receipt["retryable"] is False
    assert [item["state"] for item in receipt["files"]] == ["unknown"] * 3
    assert (artifacts / "timeout-entered.json").is_file()
    assert not (target.workspace / "src/timeout-second.txt").exists()
    (artifacts / "timeout-release").touch()
    await _wait(artifacts / "timeout-finished.json")
    actual = json.loads((artifacts / "timeout-finished.json").read_text(encoding="utf-8"))["receipt"]
    assert actual["effect"] == "completed" and [item["state"] for item in actual["files"]] == ["published"] * 3
    for name in command.expected_versions:
        assert await _read(source, command.target + "/" + name) == "TARGET " + name
    (artifacts / "timeout-source.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")


async def test_real_agent_cancellation_persists_original_unknown_tool_result(mutation_case, request):
    source, target, _, artifacts, model = mutation_case
    command = _patch(source, target, "cancel")
    session = await create_session(source.client, "取消远端文件等待")
    main = await source.client.get(f"/api/v1/sessions/{session}/main-thread")
    assert main.status_code == 200, main.text
    thread = main.json()["data"]["thread_id"]
    port = integration_port_block_for_file(Path(request.node.path)).port(10)
    cassette = build_cassette(
        asset_id="cancel-text-mutation", model="e2e-stub-model",
        url=f"http://127.0.0.1:{port}/v1/chat/completions",
        frames=chat_completion_frames(
            response_id="chatcmpl-cancel-patch", model="e2e-stub-model",
            deltas=({"role": "assistant", **_tool("apply_patch", command.model_dump(), "call_cancel_patch")},),
            finish_reason="tool_calls",
        ),
    )
    (call_id,) = model.control.plan_model_calls(session, thread, cassette.raw["interactions"])
    response = await source.client.post(f"/api/v1/sessions/{session}/messages", json={
        "message": {"content": "验证取消已发布的远端文件调用"},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert response.status_code == 200, response.text
    accepted = response.json()["data"]
    await _wait(artifacts / "cancel-entered.json", 30)
    interrupted = await source.client.post(f"/api/v1/sessions/{session}/interrupt")
    assert interrupted.status_code == 200, interrupted.text
    async with asyncio.timeout(20):
        while True:
            status = await source.client.get(f"/api/v1/jobs/{accepted['job_id']}")
            assert status.status_code == 200, status.text
            job = status.json()["data"]
            if job["status"] in {"cancelled", "failed", "completed", "timed_out", "execution_lost"}:
                break
            await asyncio.sleep(0.02)
    assert job["status"] == "cancelled", job
    recorded = json.loads((model.artifacts / f"{call_id}.json").read_text(encoding="utf-8"))
    assert recorded["identity"]["session_id"] == session
    assert recorded["identity"]["thread_id"] == thread
    events = await source.client.get(f"/api/v1/sessions/{session}/turns/{accepted['turn_id']}/message-stream/events")
    assert events.status_code == 200, events.text
    result, = [item["payload"] for item in events.json()["data"] if item["type"] == "tool.completed"]
    receipt = json.loads(result["result"])
    assert result["outcome"] == "outcome_unknown" and result["tool_name"] == "apply_patch"
    assert receipt["code"] == "cancellation_requested" and receipt["effect"] == "unknown" and receipt["retryable"] is False
    assert receipt["target"] == command.target
    assert [item["state"] for item in receipt["files"]] == ["unknown"] * 3
    assert {item["path"]: item["expected_version"] for item in receipt["files"]} == {
        command.target + "/" + name: "absent" for name in command.expected_versions}
    assert not (target.workspace / "src/cancel-second.txt").exists()
    (artifacts / "cancel-release").touch()
    await _wait(artifacts / "cancel-finished.json")
    for name in command.expected_versions:
        assert await _read(source, command.target + "/" + name) == "TARGET " + name
        assert (source.workspace / "src" / name).read_text(encoding="utf-8") == "SOURCE DECOY"
    await asyncio.to_thread(source.restart_backend)
    cold = await source.client.get(f"/api/v1/sessions/{session}/turns/{accepted['turn_id']}/message-stream/events")
    assert cold.status_code == 200, cold.text
    assert [item["payload"] for item in cold.json()["data"] if item["type"] == "tool.completed"] == [result]
    history = await source.client.post(f"/api/v1/sessions/{session}/history", json={
        "turn_ids": [accepted["turn_id"]], "tool_call_ids": [result["tool_call_id"]], "include": ["tool_call", "tool_result"],
    })
    assert history.status_code == 200, history.text
    detail, = history.json()["data"]["items"]
    persisted, = [part for part in detail["response_parts"] if part["kind"] == "tool_result"]
    assert persisted["source"]["item_id"] and persisted["tool_call_id"] == result["tool_call_id"]
    assert json.loads(persisted["result"]) == receipt
    (artifacts / "cancel-agent-events.json").write_text(json.dumps(cold.json(), ensure_ascii=False, indent=2), encoding="utf-8")
    (artifacts / "cancel-agent-history.json").write_text(json.dumps(history.json(), ensure_ascii=False, indent=2), encoding="utf-8")
