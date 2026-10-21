"""同负载存储基线：真实 Gateway/产品执行、并行工具和进程冷恢复。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from time import perf_counter_ns

import commentjson
import httpx
import pytest

from app.core.mounted_workspaces import MountedWorkspace
from app.core.path_utils import get_session_path_resolver
from app.core.workspace_identity import load_or_create_workspace_id
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.domain.itemized.serialization import normalize_wire_request
from app.services.infrastructure.rollout_context.assembly.content_store import (
    load_snapshot,
)
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.browser_manager import (
    close_browser_manager_processes,
    start_browser_manager_processes,
)
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.terminal_backend import start_terminal_backend
from tests.support.api_helpers import create_session
from tests.support.api_waiters import wait_for_job_done
from tests.support.model_stream_config import ModelStreamReplay, model_stream_replay
from tests.support.ports import integration_port_block_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.processes.workspace import (
    BackendProcess,
    close_backend_process,
    start_backend_process,
    terminate_process,
)
from tests.support.session_navigation_http import submit_navigation_operation


@dataclass
class HistoryTarget:
    workspace: Path
    backend_port: int
    gateway_port: int
    replay: ModelStreamReplay
    mount: MountedWorkspace
    backend: BackendProcess | None = None

    def restart(self) -> None:
        if self.backend is not None:
            close_backend_process(self.backend)
            self.backend = None
        self.backend = start_backend_process(
            workspace_root=str(self.workspace), port=self.backend_port,
            log_name="history-storage-backend",
            asgi_app="tests.integration.backend.sessions.rollout_context._history_storage_app:app",
            startup_mount=self.mount,
            env_overrides={
                **self.replay.environment,
                "BOXTEAM_GATEWAY_URL": f"http://127.0.0.1:{self.gateway_port}",
                "LITELLM_LOCAL_MODEL_COST_MAP": "true",
            },
        )


@pytest.fixture
def history_target(
    request: pytest.FixtureRequest,
    integration_workspace_root_path: str,
    integration_workspace_config_path: str,
) -> Iterator[HistoryTarget]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_port_block_for_file(Path(request.node.path))
    config_path = Path(integration_workspace_config_path)
    config = commentjson.loads(config_path.read_text(encoding="utf-8"))
    provider = next(p for p in config["llm"]["providers"] if p["id"] == "primary")
    provider.update({"model": "e2e-stub-model", "api_key": "history-fixture-key"})
    config["agents"]["default"]["model"] = {"primary_provider": "primary", "fallback_providers": []}
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with ExitStack() as services, model_stream_replay(
        context.output_root, runtime_root=context.runtime_root_for_node(request.node.nodeid),
        business_assertion="execution-history-storage-same-workload",
    ) as replay:
        workspace = Path(integration_workspace_root_path)
        terminal = start_terminal_backend(backend_port=ports.port(2), workspace_root=str(workspace), artifacts_dir=context.artifacts_dir)
        services.callback(terminate_process, terminal)
        browser = start_browser_manager_processes(workspace_root=workspace, artifacts_dir=context.artifacts_dir,
                                                  backend_port=ports.port(3), frontend_port=ports.port(4), backend_host="127.0.0.1")
        services.callback(close_browser_manager_processes, browser)
        mount = MountedWorkspace(load_or_create_workspace_id(workspace), workspace,
                                 terminal_backend_url=f"http://127.0.0.1:{ports.port(2)}",
                                 browser_backend_url=f"http://127.0.0.1:{ports.port(3)}")
        target = HistoryTarget(workspace, ports.port(0), ports.port(1), replay, mount)
        gateway = None
        try:
            target.restart()
            gateway = start_gateway_process(
                workspace_root=target.workspace, default_backend_url=f"http://127.0.0.1:{target.backend_port}",
                port=target.gateway_port, artifacts_dir=context.artifacts_dir / "gateway",
            )
            yield target
        finally:
            if gateway is not None:
                close_gateway_process(gateway)
            if target.backend is not None:
                close_backend_process(target.backend)


def _interaction(delta: dict[str, object], *, prompt_tokens: int = 32) -> dict[str, object]:
    return build_cassette(
        asset_id="history-storage", model="e2e-stub-model",
        url="https://opencode.ai/zen/v1/chat/completions",
        frames=chat_completion_frames(
            response_id="chatcmpl-history-storage", model="e2e-stub-model",
            deltas=({"role": "assistant", **delta},),
            finish_reason="tool_calls" if "tool_calls" in delta else "stop",
            usage={"prompt_tokens": prompt_tokens, "completion_tokens": 8, "total_tokens": prompt_tokens + 8},
        ),
    ).raw["interactions"][0]


def _storage_metrics(index: Path) -> dict[str, object]:
    """只读已提交 SQLite；dbstat 包含表和索引的实际页占用。"""
    with sqlite3.connect(index.as_uri() + "?mode=ro", uri=True) as connection:
        tables = {}
        for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
            quoted = '"' + name.replace('"', '""') + '"'
            columns = {}
            for column in connection.execute(f"PRAGMA table_info({quoted})"):
                if column[2].upper() in {"TEXT", "BLOB"}:
                    identifier = '"' + column[1].replace('"', '""') + '"'
                    columns[column[1]] = connection.execute(
                        f"SELECT coalesce(sum(length(cast({identifier} AS BLOB))),0) FROM {quoted}"
                    ).fetchone()[0]
            tables[name] = {"rows": connection.execute(f"SELECT count(*) FROM {quoted}").fetchone()[0], "column_bytes": columns}
        pages = {name: {"bytes": size, "payload_bytes": payload, "unused_bytes": unused}
                 for name, size, payload, unused in connection.execute(
                     "SELECT name,sum(pgsize),sum(payload),sum(unused) FROM dbstat GROUP BY name"
                 )}
        channels = connection.execute(
            "SELECT channel.channel_name,count(*),count(DISTINCT channel.channel_version),"
            "count(DISTINCT value_hash),coalesce(sum(value_length),0) "
            "FROM checkpoint_channels channel LEFT JOIN checkpoint_state_values value ON value.value_id=channel.value_id "
            "GROUP BY channel.channel_name ORDER BY channel.channel_name"
        ).fetchall()
        logical_view_members = connection.execute("SELECT count(*) FROM context_view_items").fetchone()[0]
    root = index.parent
    return {"tables": tables, "pages": pages, "channels": channels, "logical_view_members": logical_view_members,
            "files": {str(p.relative_to(root)): p.stat().st_size for p in root.rglob('*') if p.is_file()}}


def _verify_frozen_history(index: Path, wire: list[dict[str, object]]) -> None:
    """响应预设不能替工具成功、配对和历史请求hash作证。"""
    for record in wire:
        messages = record["request"]["messages"]
        for ordinal, message in enumerate(messages):
            calls = message.get("tool_calls", [])
            if not calls:
                continue
            results = messages[ordinal + 1:ordinal + 1 + len(calls)]
            assert [result.get("tool_call_id") for result in results] == [call["id"] for call in calls]
            assert all(result["role"] == "tool" for result in results)
            for call, result in zip(calls, results, strict=True):
                if call["function"]["name"] == "read_file":
                    assert "# 统一测试工作区" in result["content"]
                elif call["function"]["name"] == "compact_conversation":
                    assert "compaction scheduled" in result["content"]
    owners = {(record["identity"]["session_id"], record["identity"]["thread_id"]) for record in wire}
    calls = {record["identity"]["model_call_id"]: record for record in wire}
    with sqlite3.connect(index.as_uri() + "?mode=ro", uri=True) as connection:
        for session, thread, call_id, raw in connection.execute("SELECT session_id,thread_id,model_call_id,snapshot_json FROM context_assemblies"):
            assert (session, thread) in owners
            snapshot = ContextAssemblySnapshot.from_dict(load_snapshot(connection, raw))
            assert (snapshot.session_id, snapshot.thread_id) == (session, thread)
            record = calls[call_id]
            assert (record["identity"]["session_id"], record["identity"]["thread_id"]) == (session, thread)
            assert normalize_wire_request(record["request"]) == snapshot.request_hash_preimage
            snapshot.validate_hashes()


async def _complete(client: httpx.AsyncClient, session: str, content: str) -> dict[str, object]:
    accepted = await client.post(f"/api/v1/sessions/{session}/messages", json={
        "message": {"content": content},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert accepted.status_code == 200, accepted.text
    data = accepted.json()["data"]
    await wait_for_job_done(client, data["job_id"])
    return data


async def _send(target: HistoryTarget, client: httpx.AsyncClient, session: str, thread: str, turn: int) -> dict[str, object]:
    def calls(count: int, cycle: int) -> list[dict[str, object]]:
        return [{"index": i, "id": f"history-{turn}-{cycle}-{i}", "type": "function", "function": {
            "name": "read_file", "arguments": json.dumps({"path": "README.md"}),
        }} for i in range(count)]

    target.replay.control.plan_model_calls(session, thread, [
        _interaction({"tool_calls": calls(2, 0)}),
        _interaction({"tool_calls": calls(1, 1)}),
        _interaction({"content": f"历史工具循环 {turn} 完成"}),
    ])
    return await _complete(client, session, f"第{turn}轮读取 README，两次循环，保留上下文。" + "历史基线 Ω。" * 400)


def _wire(target: HistoryTarget, sessions: set[str]) -> list[dict[str, object]]:
    return [record for path in sorted(target.replay.artifacts.glob('*.json'))
            if (record := json.loads(path.read_text()))["identity"]["session_id"] in sessions]


async def _fork(client: httpx.AsyncClient, session: str, point: dict[str, object], *, mode: str, key: str) -> dict[str, object]:
    response = await client.post(f"/api/v1/sessions/{session}/fork-context", json={
        "idempotency_key": key, "source_uri": point["source_uri"],
        "source_view_id": point["source_view_id"], "source_view_revision": point["source_view_revision"],
        "anchor_item_id": point["anchor_item_id"] if mode == "view_prefix_fork" else None,
        "mode": mode, "creation_kind": "new_session_main", "target_container_uri": point["main_container_uri"],
    })
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.mark.asyncio
async def test_same_workload_tools_restart_and_storage_measurements(
    history_target: HistoryTarget, request: pytest.FixtureRequest,
) -> None:
    target = history_target
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    measurements = {"dependencies": {name: version(name) for name in ("langgraph", "langgraph-checkpoint")}, "stages": []}
    assert target.backend is not None
    async with (
        httpx.AsyncClient(base_url=f"http://127.0.0.1:{target.gateway_port}", timeout=60,
                          headers={**LOCAL_TOKEN_HEADERS, "X-Workspace-ID": target.backend.workspace_id}) as client,
        httpx.AsyncClient(base_url=f"http://127.0.0.1:{target.backend_port}", timeout=60) as observer,
    ):
        await acquire_gateway_guest(client)
        session = await create_session(client, "执行历史存储同负载")
        main = await client.get(f"/api/v1/sessions/{session}/main-thread")
        assert main.status_code == 200, main.text
        thread = main.json()["data"]["thread_id"]
        resolver = get_session_path_resolver(target.workspace / ".boxteam/sessions")
        index = resolver.resolve_thread_node(session, thread) / "rollout/index.sqlite"
        deep_turn = None
        for turn in range(3):
            started = perf_counter_ns()
            admission = await _send(target, client, session, thread, turn)
            counters = await observer.get("/__test__/history-storage-metrics")
            assert counters.status_code == 200, counters.text
            measurements["stages"].append({"turn": turn, "elapsed_ns": perf_counter_ns() - started,
                                            "operations": counters.json(), "storage": _storage_metrics(index)})
            if turn == 1:
                deep_turn = admission["turn_id"]
        history = await client.post(f"/api/v1/sessions/{session}/history", json={"direction": "head", "turns": 3})
        assert history.status_code == 200, history.text
        items = history.json()["data"]["items"]
        assert len(items) == 3
        assert [item["final_response"] for item in items] == [f"历史工具循环 {i} 完成" for i in range(3)]
        canonical = index.with_name("rollout.jsonl").read_bytes()
        await asyncio.to_thread(target.restart)
        restarted = await client.post(f"/api/v1/sessions/{session}/history", json={"direction": "head", "turns": 3})
        assert restarted.status_code == 200, restarted.text
        assert restarted.json()["data"]["items"] == items
        assert index.with_name("rollout.jsonl").read_bytes() == canonical
        started = perf_counter_ns()
        await _send(target, client, session, thread, 3)
        counters = await observer.get("/__test__/history-storage-metrics")
        assert counters.status_code == 200, counters.text
        measurements["stages"].append({"turn": 3, "after_restart": True,
                                        "elapsed_ns": perf_counter_ns() - started,
                                        "operations": counters.json(), "storage": _storage_metrics(index)})
        # 每次真实调用的封存输入都有独立产物；协议编码统一由model_stream提供。
        wire = _wire(target, {session})
        assert len(wire) == 12
        _verify_frozen_history(index, wire)
        # 从当前真实历史点击第二轮；旧revision不能跨后续执行继续使用。
        assert deep_turn is not None
        loaded = await client.post(f"/api/v1/sessions/{session}/history", json={"direction": "head", "turns": 10})
        assert loaded.status_code == 200, loaded.text
        clicked, = [item for item in loaded.json()["data"]["items"] if item["turn_id"] == deep_turn]
        final_part, = [part for part in clicked["response_parts"] if part["kind"] == "final_text"]
        point = await client.get(f"/api/v1/sessions/{session}/fork-source-point", params={
            "mode": "view_prefix_fork", "item_id": final_part["source"]["item_id"],
            "source_view_id": clicked["source_view_id"], "source_view_revision": clicked["source_view_revision"],
        })
        assert point.status_code == 200, point.text
        started = perf_counter_ns()
        child = await _fork(client, session, point.json()["data"], mode="view_prefix_fork", key="history-deep-fork")
        child_session = child["session"]["session_id"]
        child_thread = child["thread_id"]
        child_index = resolver.resolve_thread_node(child_session, child_thread) / "rollout/index.sqlite"
        point = await client.get(f"/api/v1/sessions/{child_session}/fork-source-point", params={"mode": "full_rollout_copy"})
        assert point.status_code == 200, point.text
        grandchild = await _fork(client, child_session, point.json()["data"], mode="full_rollout_copy", key="history-chain-fork")
        leaf_session = grandchild["session"]["session_id"]
        leaf_thread = grandchild["thread_id"]
        leaf_index = resolver.resolve_thread_node(leaf_session, leaf_thread) / "rollout/index.sqlite"
        measurements["stages"].append({"phase": "deep_fork_chain", "elapsed_ns": perf_counter_ns() - started,
                                        "storage": _storage_metrics(child_index), "leaf_storage": _storage_metrics(leaf_index)})
        selected = await client.post(f"/api/v1/sessions/{leaf_session}/history", json={"direction": "head", "turns": 10})
        assert selected.status_code == 200, selected.text
        frozen_items = selected.json()["data"]["items"]
        assert [item["final_response"] for item in frozen_items] == [f"历史工具循环 {i} 完成" for i in range(2)]
        # 摘要候选也由产品分配 ModelCall，不写入任何checkpoint/summary测试状态。
        summary = "历史基线摘要：已完成前三轮 README 并行读取，保留工具结果及来源身份。"
        target.replay.control.plan_model_calls(session, thread, [
            _interaction({"tool_calls": [{"index": 0, "id": "history-compact", "type": "function", "function": {
                "name": "compact_conversation", "arguments": "{}",
            }}]}, prompt_tokens=90000),
            _interaction({"content": summary}),
            _interaction({"content": "摘要后继续完成"}),
        ])
        started = perf_counter_ns()
        await _complete(client, session, "压缩已完成的历史，然后继续。")
        with sqlite3.connect(index) as connection:
            assert connection.execute("SELECT count(*) FROM item_catalog WHERE semantic_kind='compaction_summary'").fetchone() == (1,)
            assert connection.execute("SELECT count(*) FROM context_assemblies WHERE json_extract(snapshot_json,'$.selection_policy')='summary_candidate'").fetchone() == (1,)
        summary_wire = _wire(target, {session})
        assert len(summary_wire) == 15
        _verify_frozen_history(index, summary_wire)
        assert any(summary in str(record["request"]["messages"]) for record in summary_wire)
        measurements["stages"].append({"phase": "summary", "elapsed_ns": perf_counter_ns() - started,
                                        "operations": (await observer.get("/__test__/history-storage-metrics")).json(),
                                        "storage": _storage_metrics(index)})
        compacted = await client.post(f"/api/v1/sessions/{session}/history", json={"direction": "head", "turns": 10})
        assert compacted.status_code == 200, compacted.text
        await asyncio.to_thread(target.restart)
        restored_summary = await client.post(f"/api/v1/sessions/{session}/history", json={"direction": "head", "turns": 10})
        assert restored_summary.status_code == 200, restored_summary.text
        assert restored_summary.json()["data"]["items"] == compacted.json()["data"]["items"]
        (summary_restart_call,) = target.replay.control.plan_model_calls(session, thread, [_interaction({"content": "摘要重启后继续完成"})])
        await _complete(client, session, "重启后继续使用摘要。")
        summary_wire = _wire(target, {session})
        assert len(summary_wire) == 16
        continued, = [record for record in summary_wire if record["identity"]["model_call_id"] == summary_restart_call]
        assert summary in str(continued["request"]["messages"])
        _verify_frozen_history(index, summary_wire)
        measurements["stages"].append({"phase": "summary_restart_continue", "after_restart": True,
                                        "operations": (await observer.get("/__test__/history-storage-metrics")).json(),
                                        "storage": _storage_metrics(index)})
        for source in (session, child_session):
            deleted = await submit_navigation_operation(client, "delete_session", target_node_id=source, wait_for_settlement=True)
            assert (deleted["state"], deleted["settlement"]) == ("committed", "settled"), deleted
        await asyncio.to_thread(target.restart)
        restored = await client.post(f"/api/v1/sessions/{leaf_session}/history", json={"direction": "head", "turns": 10})
        assert restored.status_code == 200, restored.text
        assert restored.json()["data"]["items"] == frozen_items
        started = perf_counter_ns()
        leaf_turn = await _send(target, client, leaf_session, leaf_thread, 100)
        leaf_wire = _wire(target, {leaf_session})
        assert len(leaf_wire) == 3
        assert all(marker not in str(record["request"]["messages"]) for record in leaf_wire
                   for marker in ("第2轮读取", "第3轮读取", summary))
        # regenerate经正式replay入口回退到此Turn之前，再由产品建立新执行。
        target.replay.control.plan_model_calls(leaf_session, leaf_thread, [_interaction({"content": "回退后重新生成完成"})])
        replay = await client.post(f"/api/v1/sessions/{leaf_session}/turns/{leaf_turn['turn_id']}/replay", json={"action": "regenerate", "acknowledge_context_only": True})
        assert replay.status_code == 200, replay.text
        await wait_for_job_done(client, replay.json()["data"]["job_id"])
        history = await client.post(f"/api/v1/sessions/{leaf_session}/history", json={"direction": "head", "turns": 10})
        assert history.status_code == 200, history.text
        assert [item["final_response"] for item in history.json()["data"]["items"]] == [
            "历史工具循环 0 完成", "历史工具循环 1 完成", "回退后重新生成完成",
        ]
        wire = _wire(target, {session, child_session, leaf_session})
        assert len(wire) == 20
        _verify_frozen_history(leaf_index, wire)
        measurements["stages"].append({"phase": "source_deleted_restart_rewind", "elapsed_ns": perf_counter_ns() - started,
                                        "operations": (await observer.get("/__test__/history-storage-metrics")).json(),
                                        "storage": _storage_metrics(leaf_index)})
        measurements["wire_sha256"] = [hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest() for record in wire]
    destination = context.artifacts_dir / context.runtime_root_for_node(request.node.nodeid).parent.name / "measurements.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    assert not destination.exists(), f"拒绝覆盖已有同版本原始测量: {destination}"
    destination.write_text(json.dumps(measurements, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
