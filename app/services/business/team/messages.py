from __future__ import annotations

from app.abstractions.internal_message import PreparedInternalMessage
from app.prompting import PromptSection, internal_message_factory
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
) -> dict[str, object]:
    return {
        "team_id": team_id,
        "coordinator_session_id": coordinator_session_id,
        "role": role,
        "work_mode": work_mode,
        "tools": {
            "board": "get_team_board",
            "task_update": "update_team_task",
            "message": "send_message_to_session",
        },
    }


def membership_message(
    *, board: TeamBoardDTO, member: TeamMemberDTO
) -> PreparedInternalMessage:
    payload = trusted_team_context(
        team_id=board.team_id,
        coordinator_session_id=board.coordinator_session_id,
        role=member.role,
        work_mode=member.work_mode,
    )
    return _internal_team_message(
        kind="team_membership",
        control_payload=payload,
        data_sections=(
            (PromptSection("untrusted_instructions", member.instructions),)
            if member.instructions
            else ()
        ),
        instruction=(
            "你已作为现有会话加入团队。保留当前全部上下文和既有审查方案；"
            "以后用 get_team_board 读取团队成员与任务，"
            "用 update_team_task 更新分配给你的任务。"
        ),
        metadata={
            "source": "team_membership_attached",
            "team_id": board.team_id,
            "coordinator_session_id": board.coordinator_session_id,
            "role": member.role,
        },
    )


def task_assignment_message(
    *, board: TeamBoardDTO, task: TeamTaskDTO
) -> PreparedInternalMessage:
    return _internal_team_message(
        kind="team_task_assignment",
        control_payload={
            "team_id": board.team_id,
            "coordinator_session_id": board.coordinator_session_id,
            "required_tools": ["get_team_board", "update_team_task"],
        },
        data_sections=(PromptSection("team_task", task.model_dump(mode="json")),),
        instruction=(
            "这是团队分派任务。先调用 get_team_board 确认团队状态和依赖；"
            "完成、阻塞或失败时必须调用 update_team_task 写回任务面板。"
            "work_mode=read_only 的成员不得修改工作区文件，只能审查或测试并报告。"
        ),
        metadata={
            "source": "team_task_assignment",
            "team_id": board.team_id,
            "team_task_id": task.task_id,
            "coordinator_session_id": board.coordinator_session_id,
        },
    )


def task_update_message(
    *, board: TeamBoardDTO, task: TeamTaskDTO
) -> PreparedInternalMessage:
    return _internal_team_message(
        kind="team_task_update",
        control_payload={
            "team_id": board.team_id,
            "team_task_id": task.task_id,
            "member_session_id": task.assignee_session_id,
            "board_update_persisted": True,
        },
        data_sections=(
            PromptSection(
                "team_task_update",
                {
                    "status": task.status,
                    "summary": task.summary,
                    "error": task.error,
                },
            ),
        ),
        instruction=(
            "团队成员的这次更新已经持久化到任务面板。请调用 get_team_board 查看最新状态；"
            "当返回的面板包含本次更新时，不得声称面板尚未同步、尚未刷新或状态过期。"
            "代码修改仍由协调者主会话负责，不要在普通回复中假装成员已执行修改。"
        ),
        metadata={
            "source": "team_task_update",
            "team_id": board.team_id,
            "team_task_id": task.task_id,
            "member_session_id": task.assignee_session_id,
            "status": task.status,
        },
    )


def _internal_team_message(
    *,
    kind: str,
    control_payload: dict[str, object],
    data_sections: tuple[PromptSection, ...] = (),
    instruction: str,
    metadata: dict[str, object],
) -> PreparedInternalMessage:
    return internal_message_factory.build(
        kind=kind,
        control=instruction,
        sections=(PromptSection("control_context", control_payload), *data_sections),
        metadata=metadata,
    )
