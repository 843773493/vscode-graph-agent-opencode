"""真实单实例 stdio、配置 watcher、执行及破坏性准备失败后的修复。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import portalocker
import pytest

from app import main
from app.core.path_utils import workspace_context
from app.schemas.internal_v2.common import JobStatus
from tests.harness.python.run_context import TestRunContext
from tests.integration.backend.sessions.test_mcp_config_replacement import (
    plan_tool_loop,
    wait_status,
)
from tests.integration.backend.sessions.test_mounted_workspace_lifespan import (
    MountedCase,
    assert_closed,
)
from tests.integration.backend.sessions.test_mounted_workspace_lifespan import (
    mounted_case as mounted_case,  # noqa: PLC0414 - 复用真实双挂载装具
)
from tests.integration.backend.sessions.test_runner_cancellation import (
    RunnerCase,
    _session,
    _start_job,
)

_SERVER = r'''
import asyncio, json, os, sqlite3, sys
from pathlib import Path
import portalocker
from tests.integration.workspace_services.mcp.schema_server import serve

lock, lifecycle, definitions, receipts, database = map(Path, sys.argv[1:])
version = os.environ["MCP_TEST_VERSION"]
def record(phase):
    effects = []
    if database.exists():
        with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
            for row in connection.execute("SELECT side_effects_json FROM config_apply_journal WHERE state = 'applying'"):
                effects.extend(json.loads(row[0]))
    with lifecycle.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"phase":phase, "version":version, "pid":os.getpid(), "effects":effects}) + "\n")
with portalocker.Lock(lock, timeout=0):
    record("start")
    try:
        asyncio.run(serve(definitions, receipts))
    finally:
        record("stop")
'''


async def test_single_instance_stdio_is_stopped_before_replacement_and_failed_candidate_requires_recovery(
    mounted_case: MountedCase, inprocess_model_replay, request: pytest.FixtureRequest,
) -> None:
    replay, controller = inprocess_model_replay
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    artifacts = context.artifacts_dir
    script, lock, lifecycle, definitions, receipts = (
        artifacts / name for name in ("single_instance.py", "single.lock", "lifecycle.jsonl", "definitions.json", "receipts.jsonl")
    )
    script.write_text(_SERVER, encoding="utf-8")
    definitions.write_text(json.dumps({"probe": {"type": "object"}}), encoding="utf-8")
    root, sibling_root = mounted_case.roots.values()
    database = root / ".boxteam/state/workspace.sqlite"
    server_config = {"enabled": True, "transport": "stdio", "command": sys.executable,
                     "args": list(map(str, (script, lock, lifecycle, definitions, receipts, database))),
                     "env": {"PYTHONPATH": str(Path.cwd()), "MCP_TEST_VERSION": "old"}}
    payload = {"mcp": {"servers": {"schema": server_config}}}
    path = root / ".boxteam/workspace.jsonc"
    path.write_text(json.dumps(payload), encoding="utf-8")
    (sibling_root / ".boxteam/workspace.jsonc").write_text(json.dumps({"mcp": {"servers": {}}}), encoding="utf-8")

    async with main.lifespan(main.app):
        first, sibling = mounted_case.containers
        service, owner = first.config_service, first.mcp_catalog_owner
        original = owner.binding_snapshot()
        old = owner._runtimes["schema"]
        sibling_binding = sibling.mcp_catalog_owner.binding_snapshot()
        with pytest.raises(portalocker.exceptions.LockException), portalocker.Lock(lock, timeout=0):
            pytest.fail("真实 stdio Server 未持有单实例锁")
        server_config["env"]["MCP_TEST_VERSION"] = "new"
        path.write_text(json.dumps(payload), encoding="utf-8")
        status = await wait_status(service, lambda status: status.healthy and owner.binding_snapshot() != original)
        assert status.state == "active" and status.applied_paths == ("/mcp/servers/schema/env/MCP_TEST_VERSION",)
        assert not status.restart_required and old.scope.is_closed and not old.running
        current = owner.binding_snapshot()
        assert current.catalog_revision == original.catalog_revision
        assert current.resolve("mcp__schema__probe").connection_ref != original.resolve("mcp__schema__probe").connection_ref
        phases = [json.loads(line) for line in lifecycle.read_text().splitlines()]
        assert [(row["phase"], row["version"]) for row in phases] == [("start", "old"), ("stop", "old"), ("start", "new")]
        assert any(effect == {"resource": "mcp_connection", "action": "stop", "server_id": "schema", "connection_ref": original.resolve("mcp__schema__probe").connection_ref}
                   for effect in phases[1]["effects"])
        assert phases[0]["pid"] != phases[2]["pid"]
        with workspace_context(first.workspace_id, root, session_catalog=first.session_catalog):
            runner = RunnerCase(first, root)
            session_id, thread_id = await _session(runner)
            plan_tool_loop(replay, first, session_id, thread_id, "stdio-new")
            job = await _start_job(runner, "user", session_id, thread_id)
            await asyncio.wait_for(asyncio.shield(job.task), 30)
            assert job.status == JobStatus.completed, job.error_message
            # 旧实例确已停止，新实例 initialize 成功但目录契约无效，不能声称旧服务仍健康。
            definitions.write_text(json.dumps({"probe": {"type": "invalid-schema-type"}}), encoding="utf-8")
            server_config["env"]["MCP_TEST_VERSION"] = "broken"
            path.write_text(json.dumps(payload), encoding="utf-8")
            failed = await wait_status(service, lambda status: status.state == "recovery_required")
            assert not failed.healthy and not failed.applied_paths and not failed.deferred_paths
            assert service.get_snapshot().to_dict()["mcp"]["servers"]["schema"]["env"]["MCP_TEST_VERSION"] == "new"
            assert owner.publication_snapshot().status == "unavailable" and not owner._runtimes["schema"].running
            assert failed.apply_id is not None
            journal = first.workspace_activity_service.store.get_config_apply_journal(apply_id=failed.apply_id)
            assert journal is not None and journal.state == "recovery_required"
            assert any(effect["action"] == "stop" for effect in journal.side_effects)
            assert sibling.mcp_catalog_owner.binding_snapshot() == sibling_binding
            # 用户修改配置后仍由同一 watcher／Owner 修复，不建立另一条恢复连接链。
            definitions.write_text(json.dumps({"probe": {"type": "object"}}), encoding="utf-8")
            server_config["env"]["MCP_TEST_VERSION"] = "fixed"
            path.write_text(json.dumps(payload), encoding="utf-8")
            repaired = await wait_status(service, lambda status: status.healthy)
            assert repaired.state == "active" and not repaired.restart_required
            assert owner._runtimes["schema"].running and owner.binding_snapshot() != current
            plan_tool_loop(replay, first, session_id, thread_id, "stdio-fixed")
            job = await _start_job(runner, "user", session_id, thread_id)
            await asyncio.wait_for(asyncio.shield(job.task), 30)
            assert job.status == JobStatus.completed, job.error_message
            assert controller.transport.call_count == 4
            assert [json.loads(line) for line in receipts.read_text().splitlines()] == [
                {"name": "probe", "arguments": {"marker": "stdio-new"}},
                {"name": "probe", "arguments": {"marker": "stdio-fixed"}},
            ]
            original_failure = first.workspace_activity_service.store.get_config_apply_journal(apply_id=failed.apply_id)
            assert original_failure is not None and original_failure.state == "recovery_required"
    assert_closed(mounted_case)
    phases = [json.loads(line) for line in lifecycle.read_text().splitlines()]
    assert [(row["phase"], row["version"]) for row in phases] == [
        ("start", "old"), ("stop", "old"), ("start", "new"), ("stop", "new"),
        ("start", "broken"), ("stop", "broken"), ("start", "fixed"), ("stop", "fixed"),
    ]
    with portalocker.Lock(lock, timeout=0):
        pass
