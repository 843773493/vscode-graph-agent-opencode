"""真实 Gateway、受管服务与配置事务的故障结算及终端输出验收。"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import commentjson
import httpx
import pytest

from app.core.path_utils import get_session_path_resolver
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
from tests.support.model_stream.config import ModelStreamReplay, model_stream_replay
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)


@dataclass(frozen=True)
class LoggingCase:
    client: httpx.Client
    home: Path
    workspace: Path
    artifacts: Path
    replay: ModelStreamReplay


@pytest.fixture
def logging_case(
    request: pytest.FixtureRequest, integration_workspace_root_path: str,
) -> Iterator[LoggingCase]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    runtime = context.runtime_root_for_node(request.node.nodeid)
    artifacts = context.artifacts_dir / request.node.name
    artifacts.mkdir(parents=True, exist_ok=True)
    home = context.boxteam_home_for_node(request.node.nodeid)
    workspace = Path(integration_workspace_root_path)
    config_path = workspace / ".boxteam/workspace.jsonc"
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    provider = next(item for item in config["llm"]["providers"] if item["id"] == "primary")
    provider.update({"model": "e2e-stub-model", "api_key": "logging-fixture-key"})
    config["agents"]["default"]["model"] = {"primary_provider": "primary", "fallback_providers": []}
    config_path.write_text(json.dumps(config, ensure_ascii=False) + "\n", encoding="utf-8")
    port = integration_port_block_for_file(Path(request.node.path)).port(0)
    with model_stream_replay(
        context.output_root, runtime_root=runtime, business_assertion=request.node.nodeid,
    ) as replay:
        gateway = start_gateway_process(
            workspace_root=workspace, default_backend_url=None, port=port,
            boxteam_home=home, artifacts_dir=artifacts,
            asgi_app="tests.integration.gateway.runtime_logging_app:app",
            extra_env={**replay.environment,
                       "BOXTEAM_LOGGING_TEST_FAILURE": getattr(request, "param", "none"),
                       "BOXTEAM_LOGGING_TEST_AUDIT": str(artifacts / "control-http.jsonl")},
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=LOCAL_TOKEN_HEADERS,
                              timeout=10, trust_env=False) as client:
                client.post("/api/gateway/users/guest", json={"tracking": {"test": True}}).raise_for_status()
                yield LoggingCase(client, home, workspace, artifacts, replay)
        finally:
            close_gateway_process(gateway)


def _states(client: httpx.Client) -> dict[str, dict]:
    data = client.get("/api/gateway/diagnostics").raise_for_status().json()["data"]
    assert len(data["processes"]) == 4, data
    assert all(process["error"] is None for process in data["processes"]), data
    return {process["service"]: process["state"] for process in data["processes"]}


def _wait_status(client: httpx.Client, states: set[str]) -> dict:
    deadline = time.monotonic() + 20
    status = None
    while time.monotonic() < deadline:
        status = client.get("/api/gateway/config/reload-status").raise_for_status().json()["data"]
        if status["state"] in states:
            return status
        time.sleep(0.05)
    raise AssertionError(f"配置未结算: {status}")


def _active(database: sqlite3.Connection) -> tuple:
    result = database.execute("SELECT * FROM config_active_snapshot WHERE config_domain='gateway'").fetchone()
    assert result is not None
    return result


@pytest.mark.parametrize("logging_case", ["reject", "cas", "rollback"], indirect=True)
def test_logging_faults_settle_real_processes_and_sqlite(
    logging_case: LoggingCase, request: pytest.FixtureRequest,
) -> None:
    case = logging_case
    failure = request.node.callspec.params["logging_case"]
    before = _states(case.client)
    assert set(before) == {"gateway", "backend", "terminal", "browser"}
    assert all(state["level"] == "info" and state["pretty"] for state in before.values())
    with sqlite3.connect(case.home / "state/gateway/gateway.sqlite") as database:
        active = _active(database)
        config_path = case.home / "config/gateway.jsonc"
        document = commentjson.loads(config_path.read_text(encoding="utf-8"))
        document["logging"] = {"runtime": {"level": "debug", "pretty": False}}
        config_path.write_text(json.dumps(document) + "\n", encoding="utf-8")
        expected = "conflict" if failure == "cas" else "recovery_required" if failure == "rollback" else "rejected"
        status = _wait_status(case.client, {expected})
        assert status["revision"] == before["gateway"]["applied_revision"]
        assert _active(database) == active
        pending = database.execute(
            "SELECT state,payload_json FROM config_pending_candidate WHERE candidate_id=?", (status["candidate_id"],),
        ).fetchone()
        assert pending is not None and pending[0] == expected
        assert json.loads(pending[1])["logging"]["runtime"] == {"level": "debug", "pretty": False}
        journal = database.execute(
            "SELECT state,side_effects_json FROM config_apply_journal WHERE apply_id=?", (status["apply_id"],),
        ).fetchone()
        assert journal is not None
        assert journal[0] == ("recovery_required" if failure == "rollback" else "compensated")
        assert database.execute("SELECT count(*) FROM config_apply_claim").fetchone() == (0,)
    after = _states(case.client)
    for service, state in after.items():
        if failure == "rollback" and service == "backend":
            assert state == before[service] | {"level": "debug", "pretty": False, "applied_revision": state["applied_revision"]}
            assert state["applied_revision"] != before[service]["applied_revision"]
        else:
            assert state == before[service], service
    audit = [json.loads(line) for line in (case.artifacts / "control-http.jsonl").read_text().splitlines()]
    rejected = [(entry["service"], entry["status"]) for entry in audit if "status" in entry and entry["status"] != 200]
    assert rejected == ([] if failure == "cas" else [("browser", 409), ("backend", 409)] if failure == "rollback" else [("browser", 409)])
    if failure == "cas":
        assert status["last_error"] == "Gateway 配置应用失败: stage=promotion, error=ConfigConflictError"
        assert any(entry.get("error") == "Gateway active revision CAS 冲突" for entry in audit)
        assert [entry["service"] for entry in audit if entry.get("actual", {}).get("level") == "debug"] == ["backend", "terminal", "browser"]
    effects = json.loads(journal[1])
    assert any(effect.get("action") == ("rollback_failed" if failure == "rollback" else "restored") for effect in effects)
    (case.artifacts / "settlement.json").write_text(json.dumps({
        "status": status, "before": before, "after": after, "pending_state": pending[0],
        "journal_state": journal[0], "effects": effects, "http": audit,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_warning_keeps_real_terminal_tool_bytes_and_workspace_records(logging_case: LoggingCase) -> None:
    case = logging_case
    config_path = case.home / "config/gateway.jsonc"
    document = commentjson.loads(config_path.read_text(encoding="utf-8"))
    document["logging"] = {"runtime": {"level": "warning", "pretty": False}}
    config_path.write_text(json.dumps(document) + "\n", encoding="utf-8")
    deadline = time.monotonic() + 20
    settings: dict[str, dict] = {}
    while time.monotonic() < deadline:
        settings = _states(case.client)
        if all(state["level"] == "warning" and not state["pretty"] for state in settings.values()):
            break
        time.sleep(0.05)
    else:
        raise AssertionError(f"WARNING 未由全部实际进程确认: {settings}")
    _wait_status(case.client, {"active"})
    session = case.client.post("/api/v1/sessions", json={"title": "WARNING 原始终端输出"}).raise_for_status().json()["data"]
    session_id, thread_id = session["session_id"], session["thread_id"]
    expected = "\x1b[31m中文 DEBUG\x1b[0m\n第二行\nthird line"
    command = "printf '\\033[31m中文 DEBUG\\033[0m\\n第二行\\nthird line'; exit 7"
    replies = [{"tool_calls": [{"index": 0, "id": "logging-terminal-bytes", "type": "function", "function": {
        "name": "exec_command", "arguments": json.dumps({"cmd": command, "yield_time_ms": 10000}),
    }}]}, {"content": "已读取原始输出和退出码。"}]
    calls = case.replay.control.plan_model_calls(session_id, thread_id, [cast(list[dict[str, object]], build_cassette(
        asset_id="logging-terminal-bytes", model="e2e-stub-model",
        url="https://opencode.ai/zen/v1/chat/completions",
        frames=chat_completion_frames(
            response_id=f"chatcmpl-logging-{index}", model="e2e-stub-model",
            deltas=({"role": "assistant", **reply},),
            finish_reason="tool_calls" if "tool_calls" in reply else "stop",
        ),
    ).raw["interactions"])[0] for index, reply in enumerate(replies)])
    accepted = case.client.post(f"/api/v1/sessions/{session_id}/messages", json={
        "message": {"content": "执行命令并核对输出；退出码7是预期结果。"},
        "run": {"mode": "single_agent", "agent_id": "default"},
    }).raise_for_status().json()["data"]
    deadline = time.monotonic() + 60
    job: dict = {}
    while time.monotonic() < deadline:
        job = case.client.get(f"/api/v1/jobs/{accepted['job_id']}").raise_for_status().json()["data"]
        if job["status"] in {"completed", "succeeded"}:
            break
        assert job["status"] not in {"failed", "cancelled", "timed_out"}, job
        time.sleep(0.1)
    else:
        raise AssertionError(f"终端工具执行未结束: {job}")
    # 模型实际收到的 wire 请求、用户历史与 canonical 正文必须核对同一结果。
    wire = json.loads((case.replay.artifacts / f"{calls[1]}.json").read_text(encoding="utf-8"))["request"]
    tool_message, = [message for message in wire["messages"] if message["role"] == "tool"]
    result = json.loads(tool_message["content"])
    assert result["output"].encode("utf-8") == expected.encode("utf-8")
    assert result["exit_code"] == 7 and result["status"] == "error"
    resolver = get_session_path_resolver(case.workspace / ".boxteam/sessions")
    node = resolver.resolve_session_node(session_id)
    thread_node = resolver.resolve_thread_node(session_id, thread_id)
    records = [json.loads(line) for line in (thread_node / "rollout/rollout.jsonl").read_text(encoding="utf-8").splitlines()]
    tool_results = [record for record in records if record.get("semantic_kind") == "tool_result"]
    tool_item, = [record for record in tool_results if record["producer_ref"]["producer_kind"] == "tool"]
    assert tool_item["payload"]["tool_call_id"] == f"{calls[0]}:tool-call:logging-terminal-bytes"
    assert all(json.loads(record["payload"]["content"]) == result for record in tool_results)
    assert json.loads(tool_item["payload"]["content"]) == result
    history = case.client.post(f"/api/v1/sessions/{session_id}/history", json={"direction": "head", "turns": 10}).raise_for_status().json()["data"]
    turn, = history["items"]
    detail = case.client.post(f"/api/v1/sessions/{session_id}/history", json={
        "turn_ids": [turn["turn_id"]], "tool_call_ids": [tool_item["payload"]["tool_call_id"]],
        "include": ["tool_call", "tool_result"],
    }).raise_for_status().json()["data"]
    (case.artifacts / "terminal-history.json").write_text(json.dumps(detail, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    visible_result, = [part for item in detail["items"] for part in item["response_parts"] if part["kind"] == "tool_result"]
    assert json.loads(visible_result["result"]) == result
    assert visible_result["tool_call_id"] == tool_item["payload"]["tool_call_id"]
    logs = sorted((node / "logs/llm_requests").glob("*.json"))
    assert len(logs) == 2
    persisted = [json.loads(path.read_text(encoding="utf-8")) for path in logs]
    model_tools = [message for record in persisted for message in record["request"]["messages"] if message["type"] == "tool"]
    assert len(model_tools) == 1 and json.loads(model_tools[0]["content"]) == result
    trace = node / "logs/traces/events.jsonl"
    assert trace.stat().st_size > 0
    assert result["terminal_id"] in trace.read_text(encoding="utf-8")
    global_files = [path for path in (case.home / "state/logs").rglob("*") if path.is_file()]
    assert all(path.name != "rollout.jsonl" and "llm_requests" not in path.parts and "traces" not in path.parts for path in global_files)
    assert _states(case.client) == settings
    (case.artifacts / "terminal-bytes.json").write_text(json.dumps({
        "session_id": session_id, "thread_id": thread_id, "expected_utf8": list(expected.encode("utf-8")),
        "result": result, "processes": settings, "session_node": str(node),
        "llm_logs": [str(path) for path in logs], "trace": str(trace), "canonical_item_id": tool_item["item_id"],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
