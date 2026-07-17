from __future__ import annotations

import json

from app.schemas.public_v2.team import (
    TeamBoardDTO,
    TeamMemberDTO,
    TeamTaskDTO,
    TeamWorkMode,
)


def trusted_team_context(
    *,
    team_id: str,
    coordinator_session_id: str,
    role: str,
    work_mode: TeamWorkMode,
    instructions: str,
) -> dict[str, object]:
    return {
        "team_id": team_id,
        "coordinator_session_id": coordinator_session_id,
        "role": role,
        "work_mode": work_mode,
        "instructions": instructions,
        "tools": {
            "board": "get_team_board",
            "task_update": "update_team_task",
            "message": "send_message_to_session",
        },
    }


def membership_message(*, board: TeamBoardDTO, member: TeamMemberDTO) -> str:
    payload = trusted_team_context(
        team_id=board.team_id,
        coordinator_session_id=board.coordinator_session_id,
        role=member.role,
        work_mode=member.work_mode,
        instructions=member.instructions,
    )
    return _system_reminder(
        payload,
        "你已作为现有会话加入团队。保留当前全部上下文和既有审查方案；"
        "以后用 get_team_board 读取团队成员与任务，用 update_team_task 更新分配给你的任务。",
    )


def task_assignment_message(*, board: TeamBoardDTO, task: TeamTaskDTO) -> str:
    return _system_reminder(
        {
            "team_id": board.team_id,
            "coordinator_session_id": board.coordinator_session_id,
            "task": task.model_dump(mode="json"),
            "required_tools": ["get_team_board", "update_team_task"],
        },
        "这是团队分派任务。先调用 get_team_board 确认团队状态和依赖；"
        "完成、阻塞或失败时必须调用 update_team_task 写回任务面板。"
        "work_mode=read_only 的成员不得修改工作区文件，只能审查或测试并报告。",
    )


def task_update_message(*, board: TeamBoardDTO, task: TeamTaskDTO) -> str:
    return _system_reminder(
        {
            "team_id": board.team_id,
            "team_task_id": task.task_id,
            "member_session_id": task.assignee_session_id,
            "status": task.status,
            "summary": task.summary,
            "board_update_persisted": True,
        },
        "团队成员的这次更新已经持久化到任务面板。请调用 get_team_board 查看最新状态；"
        "当返回的面板包含本次更新时，不得声称面板尚未同步、尚未刷新或状态过期。"
        "代码修改仍由协调者主会话负责，不要在普通回复中假装成员已执行修改。",
    )


def _system_reminder(payload: dict[str, object], instruction: str) -> str:
    return (
        "<system_reminder>\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        f"{instruction}\n"
        "</system_reminder>"
    )
