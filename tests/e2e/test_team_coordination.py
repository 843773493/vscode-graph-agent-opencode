from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil

import httpx
import pytest

from tests.e2e.utils import get_trace_payload, wait_for_job_done


@pytest.fixture(scope="module")
def e2e_workspace_config_path(
    e2e_workspace_root_path: str,
    e2e_config_path: str,
) -> str:
    """本场景需要可靠工具调用，避开只返回 reasoning 的默认测试模型。"""
    source_path = Path(e2e_config_path).resolve()
    target_path = Path(e2e_workspace_root_path) / ".boxteam" / "boxteam.jsonc"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    payload = source_path.read_text(encoding="utf-8")
    payload = payload.replace(
        '"primary_provider": "primary"',
        '"primary_provider": "backup_3"',
        1,
    )
    target_path.write_text(payload, encoding="utf-8")
    shutil.copy2(
        Path.cwd().resolve() / "configs" / "config.jsonc",
        target_path.parent / "config.schema.jsonc",
    )
    return str(target_path)


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


async def _job_tool_names(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    job_id: str,
) -> list[str]:
    response = await client.get(f"/api/v1/sessions/{session_id}/traces")
    assert response.status_code == 200, response.text
    return [
        str(get_trace_payload(event).get("tool_name"))
        for event in response.json()["data"]
        if event.get("job_id") == job_id and event.get("type") == "tool_call_start"
    ]


async def _run_required_tool_turn(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    prompt: str,
    required_tool: str,
) -> str:
    job_id = await _run_turn(client, session_id=session_id, prompt=prompt)
    tool_names = await _job_tool_names(
        client,
        session_id=session_id,
        job_id=job_id,
    )
    assert required_tool in tool_names, (
        f"会话未调用要求的工具 {required_tool}: {tool_names}"
    )
    return job_id


def _team_board(workspace_root: Path, name: str) -> tuple[Path, dict[str, object]]:
    boards = []
    for path in (workspace_root / ".boxteam" / "teams").glob("team_*/team.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload["name"] == name:
            boards.append((path, payload))
    assert len(boards) == 1, f"找不到唯一团队面板 name={name}: {boards}"
    return boards[0]


async def _wait_for_review_completion(
    client: httpx.AsyncClient,
    *,
    board_path: Path,
    reviewer_session_id: str,
    max_attempts: int = 180,
) -> dict[str, object]:
    assigned_job_id = None
    for _ in range(max_attempts):
        board = json.loads(board_path.read_text(encoding="utf-8"))
        review_tasks = [
            task
            for task in board["tasks"]
            if task["assignee_session_id"] == reviewer_session_id
            and task["phase"] == "review"
        ]
        if review_tasks:
            task = review_tasks[-1]
            assigned_job_id = task.get("assigned_job_id")
            if assigned_job_id and task["status"] == "completed":
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
    parent_session_id = await _create_session(client, "团队 E2E：自动审查会话")
    team_name = "E2E 自动创建审查成员"

    await _run_required_tool_turn(
        client,
        session_id=parent_session_id,
        required_tool="apply_patch",
        prompt=(
            "必须真实调用 apply_patch，把 team-created-review.txt 的唯一一行 before 改成 after。"
            "不要调用 read_file、glob 或其他工具；成功后只回复 PATCH_OK。"
        ),
    )
    assert target.read_text(encoding="utf-8") == "after\n"
    await _run_required_tool_turn(
        client,
        session_id=parent_session_id,
        required_tool="create_team",
        prompt=(
            f"只调用 create_team 创建名称为“{team_name}”的团队。"
            "工具成功后只回复 TEAM_OK。"
        ),
    )
    board_path, board = _team_board(workspace_root, team_name)
    team_id = board["team_id"]
    member_job_id = await _run_required_tool_turn(
        client,
        session_id=parent_session_id,
        required_tool="create_team_member",
        prompt=(
            f"只调用 create_team_member：team_id={team_id}，role=reviewer，"
            "work_mode=read_only，startup_prompt=只调用 send_message_to_session 向父会话报告 "
            "REVIEWER_READY 后等待，instructions=只读审查且禁止修改文件。"
            "不要调用旧 task 工具；成功后只回复 MEMBER_OK。"
        ),
    )
    assert "task" not in await _job_tool_names(
        client,
        session_id=parent_session_id,
        job_id=member_job_id,
    )
    _, board = _team_board(workspace_root, team_name)
    reviewers = [member for member in board["members"] if member["role"] == "reviewer"]
    assert len(reviewers) == 1
    assert reviewers[0]["source"] == "delegated"
    reviewer_session_id = reviewers[0]["session_id"]
    await _run_required_tool_turn(
        client,
        session_id=parent_session_id,
        required_tool="assign_team_task",
        prompt=(
            f"只调用 assign_team_task：team_id={team_id}，"
            f"assignee_session_id={reviewer_session_id}，title=审查父会话修改，phase=review，"
            "cycle=1，start_assignee=true，depends_on_task_ids=[]。description 必须要求读取 "
            "team-created-review.txt，确认内容精确为 after，然后调用 update_team_task，"
            "以 completed 和包含 AUTO_REVIEW_OK 的 summary 写回。分派后只回复 ASSIGNED_OK。"
        ),
    )
    task = await _wait_for_review_completion(
        client,
        board_path=board_path,
        reviewer_session_id=reviewer_session_id,
    )
    assert "AUTO_REVIEW_OK" in task["summary"]
    reviewer_tools = await _job_tool_names(
        client,
        session_id=reviewer_session_id,
        job_id=task["assigned_job_id"],
    )
    assert "get_team_board" in reviewer_tools
    assert "update_team_task" in reviewer_tools


@pytest.mark.asyncio
async def test_parent_attaches_refined_manual_reviewer_without_creating_another(
    client: httpx.AsyncClient,
    e2e_workspace_root_path: str,
) -> None:
    workspace_root = Path(e2e_workspace_root_path)
    target = workspace_root / "team-attached-review.txt"
    target.write_text("draft\n", encoding="utf-8")
    reviewer_session_id = await _create_session(client, "人工调校的审查会话")
    await _run_turn(
        client,
        session_id=reviewer_session_id,
        prompt="记住审查规则：只有目标文件内容精确匹配验收值才能通过。只回复 RULE_V1_OK。",
    )
    await _run_turn(
        client,
        session_id=reviewer_session_id,
        prompt="补充审查规则：审查时必须先读团队面板，最后写回任务状态。只回复 RULE_V2_OK。",
    )
    parent_session_id = await _create_session(client, "团队 E2E：复用审查会话")
    sessions_before = await client.get("/api/v1/sessions")
    assert sessions_before.status_code == 200
    session_ids_before = {
        session["session_id"] for session in sessions_before.json()["data"]["items"]
    }
    team_name = "E2E 挂接人工审查成员"

    await _run_required_tool_turn(
        client,
        session_id=parent_session_id,
        required_tool="apply_patch",
        prompt=(
            "必须真实调用 apply_patch，把 team-attached-review.txt 的唯一一行 draft 改成 final。"
            "不要调用 read_file、glob 或其他工具；成功后只回复 PATCH_OK。"
        ),
    )
    assert target.read_text(encoding="utf-8") == "final\n"
    await _run_required_tool_turn(
        client,
        session_id=parent_session_id,
        required_tool="create_team",
        prompt=(
            f"只调用 create_team 创建名称为“{team_name}”的团队。"
            "工具成功后只回复 TEAM_OK。"
        ),
    )
    board_path, board = _team_board(workspace_root, team_name)
    team_id = board["team_id"]
    attach_job_id = await _run_required_tool_turn(
        client,
        session_id=parent_session_id,
        required_tool="attach_team_session",
        prompt=(
            f"只调用 attach_team_session：team_id={team_id}，session_id={reviewer_session_id}，"
            "role=reviewer，work_mode=read_only，notify=true，"
            "instructions=沿用该会话已经形成的严格审查方案。"
            "不要调用 create_team_member 或旧 task 工具；成功后只回复 ATTACHED_OK。"
        ),
    )
    attach_tool_names = await _job_tool_names(
        client,
        session_id=parent_session_id,
        job_id=attach_job_id,
    )
    assert "create_team_member" not in attach_tool_names
    assert "task" not in attach_tool_names
    _, board = _team_board(workspace_root, team_name)
    reviewer = next(
        member for member in board["members"] if member["session_id"] == reviewer_session_id
    )
    assert reviewer["source"] == "attached"
    await _run_required_tool_turn(
        client,
        session_id=parent_session_id,
        required_tool="assign_team_task",
        prompt=(
            f"只调用 assign_team_task：team_id={team_id}，"
            f"assignee_session_id={reviewer_session_id}，title=使用既有规则复审，"
            "phase=review，cycle=1，start_assignee=true，depends_on_task_ids=[]。"
            "description 必须要求沿用已有审查规则，读取 team-attached-review.txt，确认精确为 final，"
            "然后调用 update_team_task，以 completed 和包含 ATTACHED_REVIEW_OK 的 summary 写回。"
            "分派后只回复 ASSIGNED_OK。"
        ),
    )
    task = await _wait_for_review_completion(
        client,
        board_path=board_path,
        reviewer_session_id=reviewer_session_id,
    )
    assert "ATTACHED_REVIEW_OK" in task["summary"]

    sessions_after = await client.get("/api/v1/sessions")
    assert sessions_after.status_code == 200
    session_ids_after = {
        session["session_id"] for session in sessions_after.json()["data"]["items"]
    }
    assert session_ids_after == session_ids_before
