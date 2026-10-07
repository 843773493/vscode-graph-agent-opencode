from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncIterator, Generator
from pathlib import Path

import httpx
import pytest

from app.core.path_utils import get_session_path_resolver
from tests.support.api_waiters import wait_for_job_done
from tests.support.gateway_processes import (
    LOCAL_TOKEN_HEADERS,
    GatewayProcess,
    acquire_gateway_guest,
    close_gateway_process,
    start_gateway_process,
)
from tests.support.ports import e2e_port_block_for_file
from tests.support.trace import get_trace_payload


@pytest.fixture(scope="module")
def e2e_workspace_config_path(
    e2e_workspace_root_path: str,
    e2e_config_path: str,
) -> str:
    """本场景需要可靠工具调用，避开只返回 reasoning 的默认测试模型。"""
    source_path = Path(e2e_config_path).resolve()
    target_path = Path(e2e_workspace_root_path) / ".boxteam" / "workspace.jsonc"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    payload = source_path.read_text(encoding="utf-8")
    payload = payload.replace(
        '"primary_provider": "primary"',
        '"primary_provider": "backup_3"',
        1,
    )
    payload = payload.replace(
        '  "development": {',
        '  "runtime": {\n'
        '    "agent": {\n'
        '      "run": {"mode": "team"}\n'
        '    }\n'
        '  },\n'
        '  "development": {',
        1,
    )
    target_path.write_text(payload, encoding="utf-8")
    shutil.copy2(
        Path.cwd().resolve() / "configs" / "workspace_schema.jsonc",
        target_path.parent / "workspace_schema.jsonc",
    )
    return str(target_path)


@pytest.fixture(scope="module")
def team_gateway(
    request: pytest.FixtureRequest,
    e2e_backend_process: object,
    e2e_workspace_root_path: str,
    client_base_url: str,
) -> Generator[GatewayProcess, None, None]:
    """本文件通过真实 Gateway 代理独立 Workspace 后端。"""
    port = e2e_port_block_for_file(Path(request.node.fspath)).port(1)
    gateway = start_gateway_process(
        workspace_root=Path(e2e_workspace_root_path),
        default_backend_url=client_base_url,
        port=port,
    )
    try:
        yield gateway
    finally:
        close_gateway_process(gateway)


@pytest.fixture
async def client(team_gateway: GatewayProcess) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{team_gateway.port}",
        headers=LOCAL_TOKEN_HEADERS,
        timeout=30,
    ) as gateway_client:
        await acquire_gateway_guest(gateway_client)
        yield gateway_client


async def _create_session(client: httpx.AsyncClient, title: str) -> str:
    response = await client.post("/api/v1/sessions", json={"title": title})
    assert response.status_code == 200, response.text
    return response.json()["data"]["session_id"]


async def _run_turn(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    prompt: str,
    max_attempts: int = 180,
) -> str:
    response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "message": {"content": prompt},
            "run": {"mode": "single_agent", "agent_id": "default"},
        },
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["data"]["job_id"]
    await wait_for_job_done(client, job_id, max_attempts=max_attempts)
    return job_id


async def _job_traces(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    job_id: str,
) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    cursor = None
    while True:
        params = {"limit": 200}
        if cursor is not None:
            params["cursor"] = cursor
        response = await client.get(
            f"/api/v1/sessions/{session_id}/traces", params=params
        )
        assert response.status_code == 200, response.text
        page = response.json()["data"]
        events.extend(event for event in page["items"] if event["job_id"] == job_id)
        if not page["has_more"]:
            return events
        next_cursor = page["next_cursor"]
        assert next_cursor and next_cursor != cursor, page
        cursor = next_cursor


async def _job_tool_names(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    job_id: str,
) -> list[str]:
    return [
        str(get_trace_payload(event)["tool_name"])
        for event in await _job_traces(client, session_id=session_id, job_id=job_id)
        if event["type"] == "tool_call_start"
    ]


async def _job_tool_result(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    job_id: str,
    tool_name: str,
) -> dict[str, object]:
    results = [
        get_trace_payload(event)
        for event in await _job_traces(client, session_id=session_id, job_id=job_id)
        if event["type"] == "tool_call_end"
        and get_trace_payload(event).get("tool_name") == tool_name
    ]
    assert len(results) == 1, f"{tool_name} 必须恰有一次真实结果: {results}"
    result = results[0]
    assert result["status"] == "success" and not result["failed"], result
    payload = json.loads(result["result"])
    assert isinstance(payload, dict), payload
    return payload


async def _run_required_tool_turn(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    prompt: str,
    required_tool: str,
) -> str:
    job_id = await _run_turn(client, session_id=session_id, prompt=prompt)
    tool_names = await _job_tool_names(
        client, session_id=session_id, job_id=job_id
    )
    assert required_tool in tool_names, (
        f"会话未调用要求的工具 {required_tool}: {tool_names}"
    )
    return job_id


def _team_board_path(workspace_root: Path, session_id: str, team_id: str) -> Path:
    session_node = get_session_path_resolver(
        workspace_root / ".boxteam" / "sessions"
    ).resolve_session_node(session_id)
    path = session_node / "teams" / team_id / "team.json"
    assert path.is_file(), path
    return path


async def _child_threads(
    client: httpx.AsyncClient, session_id: str
) -> list[dict[str, object]]:
    response = await client.get(f"/api/v1/sessions/{session_id}/child-threads")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["parent_session_id"] == session_id
    assert data["total"] == len(data["items"])
    return data["items"]


async def _session_ids(client: httpx.AsyncClient) -> set[str]:
    response = await client.get("/api/v1/session-catalog/export")
    assert response.status_code == 200, response.text
    return {
        item["node_id"]
        for item in response.json()["data"]["items"]
        if item["kind"] == "session"
    }


async def _wait_for_review_completion(
    client: httpx.AsyncClient,
    *,
    board_path: Path,
    session_id: str,
    reviewer_thread_id: str,
    task_id: str,
    max_attempts: int = 180,
) -> dict[str, object]:
    assigned_job_id = None
    for _ in range(max_attempts):
        board = json.loads(board_path.read_text(encoding="utf-8"))
        task = next(task for task in board["tasks"] if task["task_id"] == task_id)
        assert task["assignee_thread_id"] == reviewer_thread_id
        assigned_job_id = task["assigned_job_id"]
        if assigned_job_id and task["status"] == "completed":
            job = await wait_for_job_done(
                client, assigned_job_id, max_attempts=max_attempts
            )
            assert job["session_id"] == session_id
            assert job["thread_id"] == reviewer_thread_id
            return task
        if task["status"] in {"failed", "cancelled"}:
            pytest.fail(f"审查任务失败: {task}")
        await asyncio.sleep(1)
    if assigned_job_id:
        response = await client.get(f"/api/v1/jobs/{assigned_job_id}")
        pytest.fail(f"审查任务未写回 completed，Job 状态: {response.text}")
    pytest.fail("团队面板没有记录审查任务的 assigned_job_id")


@pytest.mark.asyncio
async def test_parent_creates_reviewer_and_drives_review_cycle(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
) -> None:
    workspace_root = Path(e2e_workspace_root_path)
    target = workspace_root / "team-created-review.txt"
    target.write_text("before\n", encoding="utf-8")
    session_id = await _create_session(client, "团队 E2E：自动审查线程")
    sessions_before = await _session_ids(client)
    assert await _child_threads(client, session_id) == []
    team_name = "E2E 自动创建审查成员"

    await _run_required_tool_turn(
        client,
        session_id=session_id,
        required_tool="apply_patch",
        prompt=(
            "必须真实调用 apply_patch，把 team-created-review.txt 的唯一一行 before 改成 after。"
            "不要调用 read_file、glob 或其他工具；成功后只回复 PATCH_OK。"
        ),
    )
    assert target.read_text(encoding="utf-8") == "after\n"
    team_job_id = await _run_required_tool_turn(
        client,
        session_id=session_id,
        required_tool="create_team",
        prompt=(
            f"只调用 create_team 创建名称为“{team_name}”的团队。"
            "工具成功后只回复 TEAM_OK。"
        ),
    )
    board = await _job_tool_result(
        client, session_id=session_id, job_id=team_job_id, tool_name="create_team"
    )
    assert board["session_id"] == session_id and board["name"] == team_name
    team_id = board["team_id"]
    board_path = _team_board_path(workspace_root, session_id, team_id)
    member_job_id = await _run_required_tool_turn(
        client,
        session_id=session_id,
        required_tool="create_team_member",
        prompt=(
            f"只调用 create_team_member：team_id={team_id}，role=reviewer，"
            "work_mode=read_only，startup_prompt=确认只读审查角色后只回复 REVIEWER_READY，"
            "instructions=只读审查且禁止修改文件。成功后只回复 MEMBER_OK。"
        ),
    )
    assert "task" not in await _job_tool_names(
        client, session_id=session_id, job_id=member_job_id
    )
    operation = await _job_tool_result(
        client, session_id=session_id, job_id=member_job_id, tool_name="create_team_member"
    )
    reviewer_thread_id = operation["child_thread_id"]
    member = operation["member"]
    assert member["thread_id"] == reviewer_thread_id
    assert member["delegation_id"] == operation["child_delegation_id"]
    assert member["role"] == "reviewer" and member["source"] == "delegated"
    assert operation["board"]["session_id"] == session_id
    children = await _child_threads(client, session_id)
    assert len(children) == 1
    assert children[0]["thread_id"] == reviewer_thread_id
    assert children[0]["delegation_id"] == operation["child_delegation_id"]
    # 成员以控制库为权威，磁盘 team.json 不保存第二份成员投影。
    assert json.loads(board_path.read_text(encoding="utf-8"))["members"] == []

    assign_job_id = await _run_required_tool_turn(
        client,
        session_id=session_id,
        required_tool="assign_team_task",
        prompt=(
            f"只调用 assign_team_task：team_id={team_id}，"
            f"assignee_thread_id={reviewer_thread_id}，title=审查协调者修改，phase=review，"
            "cycle=1，start_assignee=true，depends_on_task_ids=[]。description 必须要求先调用 "
            "get_team_board，再读取 team-created-review.txt，确认内容精确为 after，"
            "然后调用 update_team_task，以 completed 和包含 AUTO_REVIEW_OK 的 summary 写回。"
            "分派后只回复 ASSIGNED_OK。"
        ),
    )
    assignment = await _job_tool_result(
        client, session_id=session_id, job_id=assign_job_id, tool_name="assign_team_task"
    )
    task = await _wait_for_review_completion(
        client,
        board_path=board_path,
        session_id=session_id,
        reviewer_thread_id=reviewer_thread_id,
        task_id=assignment["task"]["task_id"],
    )
    assert task["assigned_job_id"] == assignment["dispatched_job_id"]
    assert "AUTO_REVIEW_OK" in task["summary"]
    reviewer_tools = await _job_tool_names(
        client, session_id=session_id, job_id=task["assigned_job_id"]
    )
    assert "get_team_board" in reviewer_tools
    assert "update_team_task" in reviewer_tools
    assert target.read_text(encoding="utf-8") == "after\n"
    assert await _session_ids(client) == sessions_before


@pytest.mark.asyncio
async def test_parent_reuses_refined_reviewer_child_without_creating_another(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
) -> None:
    workspace_root = Path(e2e_workspace_root_path)
    target = workspace_root / "team-refined-review.txt"
    target.write_text("draft\n", encoding="utf-8")
    session_id = await _create_session(client, "团队 E2E：复用审查线程")
    sessions_before = await _session_ids(client)
    assert await _child_threads(client, session_id) == []
    team_name = "E2E 复用既有审查成员"
    team_job_id = await _run_required_tool_turn(
        client,
        session_id=session_id,
        required_tool="create_team",
        prompt=f"只调用 create_team 创建名称为“{team_name}”的团队，成功后只回复 TEAM_OK。",
    )
    board = await _job_tool_result(
        client, session_id=session_id, job_id=team_job_id, tool_name="create_team"
    )
    team_id = board["team_id"]
    board_path = _team_board_path(workspace_root, session_id, team_id)
    member_job_id = await _run_required_tool_turn(
        client,
        session_id=session_id,
        required_tool="create_team_member",
        prompt=(
            f"只调用 create_team_member：team_id={team_id}，role=reviewer，"
            "work_mode=read_only，startup_prompt=确认只读审查角色后只回复 REVIEWER_READY，"
            "instructions=只读审查且禁止修改文件。成功后只回复 MEMBER_OK。"
        ),
    )
    member = await _job_tool_result(
        client, session_id=session_id, job_id=member_job_id, tool_name="create_team_member"
    )
    reviewer_thread_id = member["child_thread_id"]
    children_before = await _child_threads(client, session_id)
    assert len(children_before) == 1
    assert children_before[0]["thread_id"] == reviewer_thread_id
    assert children_before[0]["delegation_id"] == member["child_delegation_id"]

    rules_job_id = await _run_required_tool_turn(
        client,
        session_id=session_id,
        required_tool="assign_team_task",
        prompt=(
            f"只调用 assign_team_task：team_id={team_id}，assignee_thread_id={reviewer_thread_id}，"
            "title=精炼审查规则，phase=review，cycle=1，start_assignee=true，depends_on_task_ids=[]。"
            "description=先调用 get_team_board，记住 RULE_V1：只有目标文件内容精确匹配验收值才通过；"
            "RULE_V2：审查前必须读取团队面板，完成后写回任务状态。接受两条规则后调用 "
            "update_team_task，以 completed 和包含 RULES_READY 的 summary 写回。"
            "分派后只回复 RULES_ASSIGNED。"
        ),
    )
    rules_assignment = await _job_tool_result(
        client, session_id=session_id, job_id=rules_job_id, tool_name="assign_team_task"
    )
    rules_task = await _wait_for_review_completion(
        client,
        board_path=board_path,
        session_id=session_id,
        reviewer_thread_id=reviewer_thread_id,
        task_id=rules_assignment["task"]["task_id"],
    )
    assert "RULES_READY" in rules_task["summary"]
    await _run_required_tool_turn(
        client,
        session_id=session_id,
        required_tool="apply_patch",
        prompt=(
            "必须真实调用 apply_patch，把 team-refined-review.txt 的唯一一行 draft 改成 final。"
            "不要调用 read_file、glob 或其他工具；成功后只回复 PATCH_OK。"
        ),
    )
    assert target.read_text(encoding="utf-8") == "final\n"
    review_job_id = await _run_required_tool_turn(
        client,
        session_id=session_id,
        required_tool="assign_team_task",
        prompt=(
            f"只调用 assign_team_task：team_id={team_id}，assignee_thread_id={reviewer_thread_id}，"
            "title=使用既有规则复审，phase=review，cycle=2，start_assignee=true，"
            f"depends_on_task_ids=[\"{rules_task['task_id']}\"]。description 必须要求先调用 "
            "get_team_board，核对前一审查规则任务已经 completed，沿用此前 RULE_V1 与 RULE_V2，"
            "读取 team-refined-review.txt，确认内容精确为 final，然后调用 update_team_task，"
            "以 completed 和包含 REUSED_REVIEW_OK 的 summary 写回。分派后只回复 ASSIGNED_OK。"
        ),
    )
    review_tools = await _job_tool_names(
        client, session_id=session_id, job_id=review_job_id
    )
    assert "create_team_member" not in review_tools
    assert "task" not in review_tools
    assignment = await _job_tool_result(
        client, session_id=session_id, job_id=review_job_id, tool_name="assign_team_task"
    )
    task = await _wait_for_review_completion(
        client,
        board_path=board_path,
        session_id=session_id,
        reviewer_thread_id=reviewer_thread_id,
        task_id=assignment["task"]["task_id"],
    )
    assert "REUSED_REVIEW_OK" in task["summary"]
    assert task["assigned_job_id"] != rules_task["assigned_job_id"]
    assert task["depends_on_task_ids"] == [rules_task["task_id"]]
    reviewer_tools = await _job_tool_names(
        client, session_id=session_id, job_id=task["assigned_job_id"]
    )
    assert "get_team_board" in reviewer_tools
    assert "update_team_task" in reviewer_tools
    children_after = await _child_threads(client, session_id)
    assert {child["thread_id"] for child in children_after} == {reviewer_thread_id}
    assert children_after[0]["delegation_id"] == member["child_delegation_id"]
    assert await _session_ids(client) == sessions_before
    assert target.read_text(encoding="utf-8") == "final\n"
