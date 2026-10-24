"""真实工具生成当前团队，旧持久格式只能明确拒绝，不能被读取时静默改写。"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from langchain_core.tools import BaseTool
from pydantic import ValidationError

from app.agents.tools.team import create_team_tools
from app.container import AppContainer
from app.core.identifier import create_prefixed_id
from app.core.mounted_workspaces import MountedWorkspace
from app.core.workspace_identity import load_or_create_workspace_id
from tests.harness.python.run_context import TestRunContext
from tests.support.workspace_tool_runtime import workspace_tool_runtime
from tests.support.workspaces import prepare_test_workspace


@pytest.fixture(scope="module")
def integration_workspace_root_path() -> str:
    context = TestRunContext.from_test_file(Path(__file__))
    return str(prepare_test_workspace(
        workspace_root=context.workspace_root,
        template_root=Path.cwd() / "tests/fixtures/workspaces/default_test_workspace",
        template_items=(),
    ))


@pytest.fixture
async def team_runtime(
    integration_workspace_root_path: str,
    integration_backend_port: int,
) -> AsyncIterator[tuple[AppContainer, str, dict[str, BaseTool]]]:
    root = Path(integration_workspace_root_path)
    # 本例不创建外部资源，后台监控也只能访问本测试端口块，不能触达开发服务。
    mount = MountedWorkspace(
        load_or_create_workspace_id(root), root,
        terminal_backend_url=f"http://127.0.0.1:{integration_backend_port + 60}",
        browser_backend_url=f"http://127.0.0.1:{integration_backend_port + 62}",
    )
    async with workspace_tool_runtime(root, startup_mount=mount) as (container, session_id, invocation):
        session = await container.session_service.get(session_id)
        tools = create_team_tools(
            session_id=session_id, agent_id=session.current_agent_id,
            team_service=container.team_service, invocation_context=invocation,
        )
        yield container, session_id, {tool.name: tool for tool in tools}


async def test_tools_reject_retired_session_fields_without_rewriting_ledger(
    team_runtime: tuple[AppContainer, str, dict[str, BaseTool]],
) -> None:
    container, session_id, tools = team_runtime
    created = await tools["create_team"].ainvoke({"name": "持久格式验收"})
    assigned = await tools["assign_team_task"].ainvoke({
        "team_id": created["team_id"], "assignee_thread_id": created["coordinator_thread_id"],
        "title": "当前线程自执行", "description": "验证真实工具创建的任务与只读拒绝",
        "phase": "test", "start_assignee": False,
    })
    team_id = created["team_id"]
    task_id = assigned["task"]["task_id"]
    current = await tools["get_team_board"].ainvoke({"team_id": team_id})
    assert current["session_id"] == session_id
    assert current["tasks"][0]["assignee_thread_id"] == created["coordinator_thread_id"]
    store = container.team_service._boards._store
    control = store.control(session_id)
    raw_board = control.collaboration_boards(team_id)[0]
    current_revision = control.get_collaboration_ledger_revision()
    thread_rows = control.connection.execute("SELECT * FROM thread_catalog").fetchall()
    cases = [
        ((), "coordinator_session_id", session_id),
        (("members", 0), "session_id", session_id),
        (("members", 0), "activation_job_id", "old-activation-job"),
        (("members", 0), "activation_error", "旧成员激活错误"),
        (("tasks", 0), "assignee_session_id", session_id),
        (("tasks", 0), "updated_by_session_id", session_id),
        (("recent_events", 0), "actor_session_id", session_id),
    ]
    receipts = []
    for location, retired, value in cases:
        malformed = copy.deepcopy(current)
        target = malformed
        for segment in location:
            target = target[segment]
        target[retired] = value
        encoded = json.dumps(malformed, ensure_ascii=False)
        control.connection.execute("UPDATE collaboration_boards SET board_json=? WHERE team_id=?", (encoded, team_id))
        try:
            for tool, arguments in (("get_team_board", {"team_id": team_id}), ("list_my_teams", {})):
                with pytest.raises(ValidationError) as caught:
                    await tools[tool].ainvoke(arguments)
                errors = caught.value.errors()
                assert any(error["type"] == "extra_forbidden" and error["loc"] == (*location, retired) for error in errors)
                assert control.collaboration_boards(team_id) == (encoded,)
                assert control.get_collaboration_ledger_revision() == current_revision
                assert control.connection.execute("SELECT * FROM thread_catalog").fetchall() == thread_rows
                receipts.append({"tool": tool, "field": [*location, retired], "error_type": "extra_forbidden"})
        finally:
            control.connection.execute("UPDATE collaboration_boards SET board_json=? WHERE team_id=?", (raw_board, team_id))
        assert await tools["get_team_board"].ainvoke({"team_id": team_id}) == current

    # 事件的独立读取入口也必须拒绝旧actor，不能只保护board内嵌的展示事件。
    event = store.latest_task_event(session_id, team_id, task_id)
    assert event is not None
    raw_event = control.collaboration_latest_task_event(team_id, task_id)
    assert raw_event is not None
    encoded_event = json.dumps(event.model_dump(mode="json") | {"actor_session_id": session_id})
    control.connection.execute("UPDATE collaboration_events SET event_json=? WHERE event_id=?", (encoded_event, event.event_id))
    try:
        for reader in (
            lambda: store.recent_events(session_id, team_id),
            lambda: store.latest_task_event(session_id, team_id, task_id),
        ):
            with pytest.raises(ValidationError) as caught:
                reader()
            assert [(error["loc"], error["type"]) for error in caught.value.errors()] == [(("actor_session_id",), "extra_forbidden")]
            assert control.collaboration_latest_task_event(team_id, task_id) == encoded_event
            assert control.get_collaboration_ledger_revision() == current_revision
    finally:
        control.connection.execute("UPDATE collaboration_events SET event_json=? WHERE event_id=?", (raw_event, event.event_id))
    assert await tools["get_team_board"].ainvoke({"team_id": team_id}) == current
    context = TestRunContext.from_test_file(Path(__file__)).prepare()
    (context.artifacts_dir / f"retired-field-rejections-{time.time_ns()}.json").write_text(json.dumps({
        "session_id": session_id, "thread_id": created["coordinator_thread_id"],
        "team_id": team_id, "task_id": task_id, "ledger_revision": current_revision,
        "rejections": receipts, "event_read_rejections": 2, "restored_board": current,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.parametrize("field,prefix", [("session_id", "ses"), ("team_id", "team")])
async def test_tools_reject_foreign_event_owner_without_rewriting_ledger(
    team_runtime: tuple[AppContainer, str, dict[str, BaseTool]], field: str, prefix: str,
) -> None:
    container, session_id, tools = team_runtime
    board = await tools["create_team"].ainvoke({"name": "事件归属验收"})
    team_id = board["team_id"]
    # 先由原 worker 完成创建 fanout，避免把合法异步结算误判为只读入口写入。
    await container.team_fanout_service.reconcile(session_id)
    store = container.team_service._boards._store
    control = store.control(session_id)
    event, = store.recent_events(session_id, team_id)
    raw_event, = control.collaboration_recent_events(team_id, 20)
    encoded = json.dumps(event.model_dump(mode="json") | {field: create_prefixed_id(prefix)})
    control.connection.execute(
        "UPDATE collaboration_events SET event_json=? WHERE event_id=?", (encoded, event.event_id),
    )
    tables = ("collaboration_ledger", "collaboration_boards", "collaboration_events",
              "collaboration_fanouts", "collaboration_deliveries", "session_operation_leases", "thread_catalog")

    def ledger_rows():
        return {table: control.connection.execute(f"SELECT * FROM {table}").fetchall() for table in tables}

    damaged_rows = ledger_rows()
    try:
        for tool, arguments in (("get_team_board", {"team_id": team_id}), ("list_my_teams", {})):
            with pytest.raises(RuntimeError, match="团队事件.*owner/ID"):
                await tools[tool].ainvoke(arguments)
            assert ledger_rows() == damaged_rows
        with pytest.raises(RuntimeError, match="团队事件.*owner/ID"):
            store.recent_events(session_id, team_id)
        assert ledger_rows() == damaged_rows
    finally:
        control.connection.execute(
            "UPDATE collaboration_events SET event_json=? WHERE event_id=?", (raw_event, event.event_id),
        )
    assert await tools["get_team_board"].ainvoke({"team_id": team_id}) == board
    assert (await tools["list_my_teams"].ainvoke({}))["items"] == [board]
