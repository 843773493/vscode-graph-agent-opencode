"""确定性模型回放下，真实 Workbench/Gateway/Backend 的压缩及派生控制纵切。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import commentjson
import httpx
import pytest

from app.core.path_utils import get_session_path_resolver
from app.core.session_control_store import SessionControlStore
from app.core.session_thread_context import session_thread_owner_scope
from app.core.sqlite_state import sqlite_file_uri
from app.core.workspace_identity import read_workspace_id
from app.domain.itemized.mutation_intents import MutationIntentOwner
from app.services.infrastructure.rollout_context.assembly.detail_identity import (
    detail_ref_from_key,
)
from app.services.infrastructure.rollout_context.checkpoint.saver import (
    RolloutCheckpointSaver,
)
from tests.harness.python.run_context import TestRunContext
from tests.harness.python.web_workbench import (
    web_workbench_assets as web_workbench_assets,  # noqa: PLC0414
)
from tests.integration.clients.web.test_basic_chat_tool_loop import (
    chat_backend as chat_backend,  # noqa: PLC0414
)
from tests.integration.clients.web.test_basic_chat_tool_loop import (
    chat_backend_boxteam_home as chat_backend_boxteam_home,  # noqa: PLC0414
)
from tests.integration.clients.web.test_basic_chat_tool_loop import (
    chat_loop_web_assets as chat_loop_web_assets,  # noqa: PLC0414
)
from tests.support.paths import chromium_temporary_environment
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


@pytest.fixture(scope="module", autouse=True)
def compaction_config(integration_workspace_config_path):
    path = Path(integration_workspace_config_path)
    config = commentjson.loads(path.read_text(encoding="utf-8"))
    config["profiles"]["provider.primary"]["settings"]["model_options"]["api_mode"]["model_info"]["max_input_tokens"] = 34000
    config["settings"]["workspace"].setdefault("ui", {})["auto_summarize"] = True
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def test_compaction_and_derived_scope_through_real_workbench(
    request, chat_backend, chat_backend_boxteam_home, chat_loop_web_assets,
    integration_workspace_config_path, prepared_developer_tools_package,
):
    backend_url, workspace, replay = chat_backend
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    ports = integration_ports_for_file(Path(request.node.path))
    copy_prepared_developer_tools_package(prepared_developer_tools_package, chat_backend_boxteam_home)
    gateway = start_gateway_process(
        workspace_root=workspace, default_backend_url=backend_url, port=ports.port(20),
        boxteam_home=chat_backend_boxteam_home, artifacts_dir=context.artifacts_dir / "gateway",
        extra_env={"BOXTEAM_WEB_ASSETS": str(chat_loop_web_assets)},
    )
    chromium = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH") or shutil.which("chromium")
    assert chromium, "产品纵切需要真实 Chromium"
    result_path = context.artifacts_dir / "browser-result.json"
    supervisor = context.project_root / "app/runtime" / ("windows_job.py" if sys.platform == "win32" else "posix_job.py")
    command = [sys.executable, str(supervisor), "--memory-limit-kb", "4194304", "--timeout-seconds", "300", "--",
               "node", "tests/integration/clients/web/itemized_context/compaction_derived_execution.mjs"]
    (context.artifacts_dir / "process-owner.json").write_text(json.dumps({
        "workspace": str(workspace), "home": str(chat_backend_boxteam_home), "gateway_pid": gateway.process.pid,
        "gateway_port": gateway.port, "backend_url": backend_url, "web_assets": str(chat_loop_web_assets),
        "web_main_sha256": hashlib.sha256((chat_loop_web_assets / "out/main.js").read_bytes()).hexdigest(),
    }, indent=2), encoding="utf-8")
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{gateway.port}",
                                     headers=LOCAL_TOKEN_HEADERS, timeout=60) as client:
            health = await client.get("/api/gateway/health")
            health.raise_for_status()
            await enable_developer_tools_plugins(client, gateway_id=health.json()["data"]["gateway_id"],
                workspace_id=read_workspace_id(workspace), plugin_ids=("terminal", "browser"))
        with chromium_temporary_environment(context.runtime_root_for_node(request.node.nodeid) / "chromium-tmp") as temporary:
            result = await asyncio.to_thread(subprocess.run, command, cwd=context.project_root,
                capture_output=True, text=True, timeout=315, check=False, env={**os.environ, **temporary, **replay.environment,
                    "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": chromium,
                    "BOXTEAM_BROWSER_BASE_URL": f"http://127.0.0.1:{gateway.port}",
                    "BOXTEAM_BROWSER_RESULT_PATH": str(result_path),
                    "BOXTEAM_BROWSER_MODEL_ENDPOINT": f"http://127.0.0.1:{ports.port(10)}/v1/chat/completions",
                    "BOXTEAM_BROWSER_ARTIFACT_ROOT": str(context.artifacts_dir),
                    "BOXTEAM_BROWSER_MODEL_ARTIFACTS": str(replay.artifacts),
                    "BOXTEAM_BROWSER_CONFIG_PATH": integration_workspace_config_path,
                })
    finally:
        await asyncio.to_thread(close_gateway_process, gateway)
    (context.artifacts_dir / "browser.stdout.raw").write_text(result.stdout, encoding="utf-8")
    (context.artifacts_dir / "browser.stderr.raw").write_text(result.stderr, encoding="utf-8")
    (context.artifacts_dir / "browser-process.json").write_text(json.dumps({"command": command, "exit_code": result.returncode}, indent=2), encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    facts = json.loads(result_path.read_text())
    assert facts["phase"] == "complete" and facts["pageErrors"] == []
    session, thread = facts["owner"]["session_id"], facts["owner"]["thread_id"]
    resolver = get_session_path_resolver(workspace / ".boxteam/sessions")
    node = resolver.resolve_thread_node(session, thread)
    child_thread = facts["derived"]["childThreadId"]
    child_node = resolver.resolve_thread_node(session, child_thread)
    assert node != child_node
    assert node == resolver.resolve_session_node(session) / "threads" / thread
    control = SessionControlStore(resolver.resolve_session_node(session) / "session-control.sqlite", read_only=True)
    try:
        locator = control.get_published_child_thread_locator(child_thread)
        assert child_node == resolver.resolve_session_node(session) / locator
        assert child_node.name == child_thread
    finally:
        control.close()
    with RolloutCheckpointSaver(workspace / ".boxteam/sessions") as saver:
        with saver._storage.storage.read_connection(session, thread, "") as connection:
            applied = connection.execute("SELECT candidate_id,applied_view_id FROM context_compaction_candidates WHERE applied_view_id IS NOT NULL").fetchall()
            assert len(applied) == 1
            assert connection.execute("SELECT COUNT(*) FROM model_calls WHERE execution_id LIKE 'summary-execution:%'").fetchone() == (1,)
        logs = [json.loads(path.read_text()) for path in (resolver.resolve_session_node(session) / "logs/llm_requests").glob("*.json")]
        with closing(sqlite3.connect(sqlite_file_uri(resolver.resolve_session_node(session) / "logs/traces/trace.sqlite") + "?mode=ro", uri=True)) as trace:
            trace_rows = trace.execute("SELECT job_id,thread_id,event_type,raw FROM trace_events ORDER BY start").fetchall()
        retained_inputs = {}
        for owner_thread, receipt, expected_text in (
            (thread, facts["derived"]["completed"][0], "DERIVED_PRIVATE_RESULT_MAIN"),
            (child_thread, facts["derived"]["child"], "DERIVED_PRIVATE_RESULT_CHILD"),
        ):
            request_input = saver._storage.executions.internal_execution_request_input(
                session, owner_thread, receipt["result"]["execution_id"])
            assert request_input["kind"] == "derived_reference"
            with saver._storage.storage.read_connection(session, owner_thread, "") as connection:
                row = connection.execute("SELECT session_id,thread_id,turn_id,execution_origin FROM executions WHERE execution_id=?",
                    (receipt["result"]["execution_id"],)).fetchone()
                assert row == (session, owner_thread, None, "internal")
                retained = connection.execute("SELECT reference_id FROM retention_refs WHERE retention_id=?",
                    (f"derived-input:{request_input['reference']['plan_id']}",)).fetchone()
                assert retained is not None
            frozen = saver.read_derived_input(session, owner_thread, detail_ref_from_key(retained[0]))
            assert frozen.task_id == receipt["task_id"]
            retained_inputs[owner_thread] = detail_ref_from_key(retained[0])
            output_refs = receipt["result"]["output_refs"]
            outputs = saver.read_canonical_items(session, owner_thread, item_ids=[ref["item_id"] for ref in output_refs])
            assert {(item.item_id, item.content_hash) for item in outputs} == {
                (ref["item_id"], ref["content_hash"]) for ref in output_refs
            }
            assert [item.payload for item in outputs] == [[{"type": "text", "text": expected_text}]]
            matching_logs = [log for log in logs if log["request"]["execution_id"] == receipt["result"]["execution_id"]]
            assert len(matching_logs) == 1
            logged = matching_logs[0]["request"]
            assert (logged["session_id"], logged["thread_id"], logged["turn_id"], logged["purpose"]) == (
                session, owner_thread, None, "derived",
            )
            assert expected_text in json.dumps(matching_logs[0]["response"])
            events = [(row[1], row[2], json.loads(row[3])) for row in trace_rows if row[0] == logged["job_id"]]
            assert {row[0] for row in events} == {owner_thread}
            assert [row[1] for row in events] == ["job_created", "agent_start", "llm_request", "job_completed"]
            assert events[0][2]["payload"]["message_metadata"]["execution_id"] == receipt["result"]["execution_id"]
            assert events[0][2]["payload"]["message_metadata"]["task_id"] == receipt["task_id"]
            if owner_thread == child_thread:
                assert {(ref.session_id, ref.thread_id) for ref in frozen.refs} == {(session, thread)}
                assert all((ref["session_id"], ref["thread_id"]) == (session, child_thread)
                           for ref in receipt["result"]["output_refs"])
            with saver._context_reader.open_snapshot(session, owner_thread) as snapshot:
                active = saver._storage.checkpoints.committed_context_items(
                    snapshot, predecessor=None, include_pending_notices=False,
                )
                assert not {ref["item_id"] for ref in receipt["result"]["output_refs"]}.intersection(
                    item.item_id for item in active)
                assert not any("DERIVED_PRIVATE_RESULT" in str(item.payload) for item in active)
        with session_thread_owner_scope(MutationIntentOwner(session, thread), langgraph_thread_id=thread):
            pruning = saver.plan_pruning(thread)
            assert pruning.candidates
            assert saver.execute_pruning(thread, pruning) == tuple(candidate.checkpoint_id for candidate in pruning.candidates)
        # 现行 pruning 只标记未引用 checkpoint，不物理清除仍可寻址的结果和冻结输入。
        for owner_thread, receipt in ((thread, facts["derived"]["completed"][0]),
                                      (child_thread, facts["derived"]["child"])):
            refs = receipt["result"]["output_refs"]
            assert {item.item_id for item in saver.read_canonical_items(
                session, owner_thread, item_ids=[ref["item_id"] for ref in refs],
            )} == {ref["item_id"] for ref in refs}
            assert saver.read_derived_input(session, owner_thread, retained_inputs[owner_thread]).task_id == receipt["task_id"]
        assert node.is_dir() and child_node.is_dir()
        deleted_session = facts["derived"]["deleted"]["session_id"]
        with pytest.raises(KeyError):
            resolver.resolve_session_node(deleted_session)
        manual = facts["compaction"]["manual"]["owner"]
        with saver._storage.storage.read_connection(manual["session_id"], manual["thread_id"], "") as connection:
            requested, applied_view = connection.execute(
                "SELECT apply_on_next_request,applied_view_id FROM context_compaction_candidates",
            ).fetchone()
            assert requested == 1 and applied_view is not None
            assert connection.execute(
                "SELECT COUNT(*) FROM model_calls WHERE execution_id LIKE 'summary-execution:%'",
            ).fetchone() == (1,)
    assert facts["compaction"]["summaryCalls"] == 1
    assert facts["derived"]["cancelled"]["status"] == "cancelled"
    assert facts["compaction"]["capacityFailure"]["status"] == "failed"
