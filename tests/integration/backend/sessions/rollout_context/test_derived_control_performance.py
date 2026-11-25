"""100k 物理历史、有界活动选集下，经真实 Gateway 验收派生控制延迟。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from statistics import quantiles
from time import perf_counter
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.core.checkpoint_config import build_checkpoint_config
from app.core.execution_admission_context import execution_admission_scope
from app.core.session_thread_context import session_thread_owner_scope
from app.core.workspace_identity import read_workspace_id
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.checkpoint.message_codec import (
    LangChainMessageCodec,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.harness.python.run_context import TestRunContext
from tests.integration.clients.web.test_basic_chat_tool_loop import (
    chat_backend as chat_backend,  # noqa: PLC0414
)
from tests.integration.clients.web.test_basic_chat_tool_loop import (
    chat_backend_boxteam_home as chat_backend_boxteam_home,  # noqa: PLC0414
)
from tests.support.e2e_control.harness import control_request
from tests.support.ports import integration_ports_for_file
from tests.support.processes.gateway import (
    LOCAL_TOKEN_HEADERS,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.terminal_plugin import (
    copy_prepared_developer_tools_package,
    enable_developer_tools_plugins,
)


@pytest.mark.asyncio
async def test_large_history_derived_control_through_gateway(
    request, chat_backend, chat_backend_boxteam_home, prepared_developer_tools_package,
):
    backend_url, workspace, replay = chat_backend
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_ports_for_file(Path(request.node.path))
    source_paths = [Path(__file__),
                    *(Path.cwd() / path for path in (
                        "app/api/derived_executions.py", "app/services/business/derived_execution_service.py",
                        "app/services/orchestration/background_work/derived_execution.py",
                        "app/services/orchestration/background_work/registry.py", "app/agents/model_call_execution.py",
                        "app/agents/model_call_graph.py", "app/runtime/derived_tools.py",
                        "app/domain/itemized/plans/request.py", "app/domain/itemized/assembly_snapshot.py",
                        "app/domain/itemized/derived_execution.py")),
                    *sorted((Path.cwd() / "app/services/infrastructure/rollout_context").rglob("*.py")),
                    *sorted((Path.cwd() / "app/services/infrastructure/rollout_context/storage/migrations").rglob("*.sql"))]
    before_sources = {str(path.relative_to(Path.cwd())): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in source_paths}
    copy_prepared_developer_tools_package(prepared_developer_tools_package, chat_backend_boxteam_home)
    gateway = start_gateway_process(workspace_root=workspace, default_backend_url=backend_url,
        port=ports.port(20), boxteam_home=chat_backend_boxteam_home,
        artifacts_dir=context.artifacts_dir / "gateway")
    measurements, model_waits, armed = [], [], set()
    report = {"boundary": "真实Gateway/Backend任务；原Saver追加100k后rewind，仅模型用精确回放。",
              "samples": measurements, "model_waits_ms": model_waits, "sources_before": before_sources}
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway.port}",
                                     headers=LOCAL_TOKEN_HEADERS, timeout=30) as client:
            async def api(method, path, *, label=None, **kwargs):
                request_id = str(uuid4())
                started = perf_counter()
                response = await client.request(method, path, headers={"X-Request-ID": request_id}, **kwargs)
                elapsed_ms = (perf_counter() - started) * 1000
                if label is not None:
                    measurements.append({"operation": label, "elapsed_ms": elapsed_ms,
                        "request_id": request_id, "response_bytes": len(response.content), "status": response.status_code})
                assert response.status_code == 200, response.text
                body = response.json()
                assert response.headers["X-Request-ID"] == body["request_id"] == request_id
                return body["data"]

            health = await api("GET", "/api/gateway/health")
            client.headers.update({"X-BoxTeam-Target-Gateway-Id": health["gateway_id"],
                                   "X-BoxTeam-Workspace-Id": read_workspace_id(workspace)})
            await enable_developer_tools_plugins(client, gateway_id=health["gateway_id"],
                workspace_id=read_workspace_id(workspace), plugin_ids=("terminal", "browser"))
            session = await api("POST", "/api/v1/sessions", json={"title": "100k派生控制性能"})
            session_id, thread_id = session["session_id"], session["thread_id"]
            await api("PATCH", f"/api/v1/sessions/{session_id}",
                      json={"model": {"provider_id": "primary", "model_id": "default"}})
            seed_started = perf_counter()
            with RolloutCheckpointSaver(workspace / ".boxteam/sessions") as saver:
                accepted = saver.accept_turn(session_id, thread_id=thread_id, accepted_ingress_id="perf-source",
                    acceptance_idempotency_key="perf-source", job_id="perf-source", admission_idempotency_key="perf-source",
                    payload="性能选集保留这一条事实。", payload_kind="text")
                admission = saver.execution_admission(session_id, thread_id=thread_id,
                    execution_id=accepted["initial_execution_id"])
                with (session_thread_owner_scope(MutationIntentOwner(session_id, thread_id), langgraph_thread_id=thread_id),
                      execution_admission_scope(admission, langgraph_thread_id=thread_id)):
                    checkpoint = empty_checkpoint()
                    config = saver.put(build_checkpoint_config(thread_id), checkpoint, {"source": "loop", "step": 0}, {})
                    codec = LangChainMessageCodec()
                    timestamp = datetime.now(UTC).isoformat()
                    for start in range(0, 100_000, 5000):
                        items = tuple(item for index in range(start, start + 5000)
                            for item in codec.items_for_message(AIMessage(content=f"物理旧历史{index}", id=f"perf-old-{index}"),
                                item_sequence=1, message_id=f"perf-old-{index}", turn_id=admission.turn_id,
                                execution_id=admission.execution_id, timestamp=timestamp))
                        saver.append_items(session_id, items, thread_id=thread_id)
                    saver.rewind(config, checkpoint_id=checkpoint["id"])
                # seed 只构造物理规模；经原 owner 取消准入，避免留下无法重启的假队列任务。
                saver.converge_execution(session_id, thread_id=thread_id, turn_id=admission.turn_id,
                    execution_id=admission.execution_id, outcome="cancelled", turn_status="cancelled")
                with saver._storage.storage.read_connection(session_id, thread_id, "") as connection:
                    physical = connection.execute("SELECT COUNT(*) FROM item_catalog").fetchone()[0]
                    active = connection.execute("SELECT COUNT(*) FROM context_view_items member "
                        "JOIN branches branch ON branch.head_view_id=member.view_id WHERE branch.status='active' "
                        "AND member.visible=1 AND member.context_selected=1 AND member.source_kind='canonical'").fetchone()[0]
                assert physical >= 100_000 and 1 <= active <= 8
            report.update(physical_items=physical, active_items=active, seed_ms=(perf_counter() - seed_started) * 1000)
            calls = control_request(json.loads(replay.environment["BOXTEAM_E2E_TEST_CONTROL"]), {"action": "plan_chat",
                "session_id": session_id, "thread_id": thread_id, "model": "browser-chat-tool-loop-stub",
                "url": f"http://127.0.0.1:{ports.port(10)}/v1/chat/completions",
                "responses": [{"response_id": f"performance-{index}", "deltas": [
                    {"role": "assistant", "content": f"独立性能结果{index}"}], "finish_reason": "stop"} for index in range(21)]})
            submission = {"session_id": session_id, "thread_id": thread_id, "instruction": "仅分析冻结事实。",
                          "timeout_seconds": 90, "max_model_calls": 1, "allowed_tool_ids": []}
            def query(task_id):
                return f"/api/v1/derived-executions/{task_id}?session_id={session_id}&thread_id={thread_id}"

            warmup = await api("POST", "/api/v1/derived-executions", label="cold-submit",
                               json={**submission, "idempotency_key": "performance-warmup"})
            wait_started = perf_counter()
            async with asyncio.timeout(30):
                while True:
                    completed = await api("GET", query(warmup["task_id"]) + "&wait_seconds=1")
                    assert completed["status"] not in {"failed", "cancelled", "reconcile_required"}, completed
                    if completed["status"] == "completed":
                        break
            model_waits.append((perf_counter() - wait_started) * 1000)
            assert completed["result"]["output_ref_count"] > 0
            report["completed_warmup"] = completed
            for index, call in enumerate(calls[1:]):
                replay.control.arm(call, "model.response_ready")
                armed.add(call)
                accepted = await api("POST", "/api/v1/derived-executions", label="submit",
                    json={**submission, "idempotency_key": f"performance-{index}"})
                wait_started = perf_counter()
                await asyncio.to_thread(replay.control.acknowledged, call, "model.response_ready", 30)
                model_waits.append((perf_counter() - wait_started) * 1000)
                running = await api("GET", query(accepted["task_id"]), label="get")
                assert running["status"] == "running"
                cancelled = await api("POST", f"/api/v1/derived-executions/{accepted['task_id']}/cancel",
                    label="cancel", json={"session_id": session_id, "thread_id": thread_id})
                assert cancelled["status"] == "cancelled" and cancelled["result"] is None
                replay.control.release(call, "model.response_ready")
                armed.remove(call)
                assert (await api("GET", query(accepted["task_id"])))["status"] == "cancelled"
            statistics = {}
            for operation in ("submit", "get", "cancel"):
                samples = [sample["elapsed_ms"] for sample in measurements if sample["operation"] == operation]
                assert len(samples) == 20
                statistics[operation] = {"p95_ms": quantiles(samples, n=100, method="inclusive")[94], "max_ms": max(samples)}
            report["statistics"] = statistics
            assert len(list(replay.artifacts.glob("*.json"))) == 21
            assert all(row["p95_ms"] <= 500 and row["max_ms"] <= 1500 for row in statistics.values()), statistics
            assert measurements[0]["elapsed_ms"] <= 3000
    finally:
        for call in armed:
            replay.control.release(call, "model.response_ready")
        await asyncio.to_thread(close_gateway_process, gateway)
        report["sources_after"] = {path: hashlib.sha256((Path.cwd() / path).read_bytes()).hexdigest() for path in before_sources}
        context.performance_dir.mkdir(parents=True, exist_ok=True)
        (context.performance_dir / "derived-control-performance.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert report["sources_before"] == report["sources_after"], "性能期间相关源码发生变化"
