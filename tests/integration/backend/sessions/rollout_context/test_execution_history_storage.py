"""同负载存储基线：真实 Gateway/产品执行、并行工具和进程冷恢复。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from time import perf_counter_ns

import commentjson
import httpx
import pytest

from app.core.path_utils import get_session_path_resolver
from app.domain.itemized.assembly_snapshot import ContextAssemblySnapshot
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.harness.python.run_context import TestRunContext
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
)


@dataclass
class HistoryTarget:
    workspace: Path
    backend_port: int
    gateway_port: int
    replay: ModelStreamReplay
    backend: BackendProcess | None = None

    def restart(self) -> None:
        if self.backend is not None:
            close_backend_process(self.backend)
            self.backend = None
        self.backend = start_backend_process(
            workspace_root=str(self.workspace), port=self.backend_port,
            log_name="history-storage-backend",
            asgi_app="tests.integration.backend.sessions.rollout_context._history_storage_app:app",
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
    with model_stream_replay(
        context.output_root, runtime_root=context.runtime_root_for_node(request.node.nodeid),
        business_assertion="execution-history-storage-same-workload",
    ) as replay:
        target = HistoryTarget(Path(integration_workspace_root_path), ports.port(0), ports.port(1), replay)
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


def _interaction(delta: dict[str, object]) -> dict[str, object]:
    return build_cassette(
        asset_id="history-storage", model="e2e-stub-model",
        url="https://opencode.ai/zen/v1/chat/completions",
        frames=chat_completion_frames(
            response_id="chatcmpl-history-storage", model="e2e-stub-model",
            deltas=({"role": "assistant", **delta},),
            finish_reason="tool_calls" if "tool_calls" in delta else "stop",
            usage={"prompt_tokens": 32, "completion_tokens": 8, "total_tokens": 40},
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
            "SELECT channel_name,count(*),count(DISTINCT channel_version),"
            "count(DISTINCT value_hash),coalesce(sum(value_length),0) "
            "FROM checkpoint_channels GROUP BY channel_name ORDER BY channel_name"
        ).fetchall()
    root = index.parent
    return {"tables": tables, "pages": pages, "channels": channels,
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
            assert all(result["role"] == "tool" and "# 统一测试工作区" in result["content"] for result in results)
    owners = {(record["identity"]["session_id"], record["identity"]["thread_id"]) for record in wire}
    with sqlite3.connect(index.as_uri() + "?mode=ro", uri=True) as connection:
        for session, thread, raw in connection.execute("SELECT session_id,thread_id,snapshot_json FROM context_assemblies"):
            assert (session, thread) in owners
            snapshot = ContextAssemblySnapshot.from_dict(json.loads(raw))
            assert (snapshot.session_id, snapshot.thread_id) == (session, thread)
            snapshot.validate_hashes()


async def _send(target: HistoryTarget, client: httpx.AsyncClient, session: str, thread: str, turn: int) -> None:
    def calls(count: int, cycle: int) -> list[dict[str, object]]:
        return [{"index": i, "id": f"history-{turn}-{cycle}-{i}", "type": "function", "function": {
            "name": "read_file", "arguments": json.dumps({"path": "README.md"}),
        }} for i in range(count)]

    target.replay.control.plan_model_calls(session, thread, [
        _interaction({"tool_calls": calls(2, 0)}),
        _interaction({"tool_calls": calls(1, 1)}),
        _interaction({"content": f"历史工具循环 {turn} 完成"}),
    ])
    accepted = await client.post(f"/api/v1/sessions/{session}/messages", json={
        "message": {"content": f"第{turn}轮读取 README，两次循环，保留上下文。" + "历史基线 Ω。" * 400},
        "run": {"mode": "single_agent", "agent_id": "default"},
    })
    assert accepted.status_code == 200, accepted.text
    await wait_for_job_done(client, accepted.json()["data"]["job_id"])


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
        for turn in range(3):
            started = perf_counter_ns()
            await _send(target, client, session, thread, turn)
            counters = await observer.get("/__test__/history-storage-metrics")
            assert counters.status_code == 200, counters.text
            measurements["stages"].append({"turn": turn, "elapsed_ns": perf_counter_ns() - started,
                                            "operations": counters.json(), "storage": _storage_metrics(index)})
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
        wire = [record for path in target.replay.artifacts.glob('*.json')
                if (record := json.loads(path.read_text()))["identity"]["session_id"] == session
                and record["identity"]["thread_id"] == thread]
        assert len(wire) == 12
        _verify_frozen_history(index, wire)
        measurements["wire_sha256"] = [hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest() for record in wire]
    destination = context.artifacts_dir / context.runtime_root_for_node(request.node.nodeid).parent.name / "measurements.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    assert not destination.exists(), f"拒绝覆盖已有同版本原始测量: {destination}"
    destination.write_text(json.dumps(measurements, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
