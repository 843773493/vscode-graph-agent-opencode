from __future__ import annotations

from typing import Annotated, Literal

from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field, StrictBool

from app.abstractions.team import TeamCoordinationProtocol
from app.agents.tool_invocation_context import ToolInvocationContext
from app.core.job_context import get_current_job_id


TEAM_COORDINATION_NOTICE = (
    "团队任务采用事件驱动协作：成员完成、阻塞或失败后会更新团队面板，并自动为协调者启动通知 Job。"
    "不要对团队成员调用 monitor_session_agent_end 或 collect_background_messages；当前 Job 应在分派后结束，"
    "收到团队更新通知时再调用 get_team_board 汇总最新状态。"
)


def _operation_payload(result: BaseModel) -> dict[str, object]:
    payload = result.model_dump(mode="json")
    payload["coordination_notice"] = TEAM_COORDINATION_NOTICE
    return payload


class CreateTeamInput(BaseModel):
    name: str = Field(description="团队名称，例如：认证模块交付团队")


class TeamIdInput(BaseModel):
    team_id: str = Field(description="目标团队 ID")


class AttachTeamSessionInput(TeamIdInput):
    session_id: str = Field(description="用户提供的现有 Session ID")
    role: str = Field(description="加入团队后的角色，例如 reviewer")
    instructions: str = Field(
        default="",
        description="补充团队职责；不会覆盖该 Session 已有上下文和对话历史",
    )
    work_mode: Literal["write", "read_only"] = Field(
        default="read_only",
        description="团队职责模式；read_only 是可信任务约束，不是操作系统级文件沙箱",
    )
    notify: StrictBool = Field(
        default=True,
        description="是否立即向现有 Session 发送可信团队激活消息",
    )


class AssignTeamTaskInput(TeamIdInput):
    assignee_session_id: str = Field(description="任务负责人 Session ID")
    title: str = Field(description="任务标题")
    description: str = Field(description="可独立执行的完整任务说明和验收标准")
    phase: Literal["development", "review", "test", "fix", "other"]
    cycle: int = Field(default=1, ge=1, description="开发—审查—测试循环编号")
    depends_on_task_ids: list[str] = Field(
        default_factory=list,
        description="必须已经 completed 的前置团队任务 ID",
    )
    start_assignee: StrictBool = Field(
        default=True,
        description=(
            "是否立即给负责人 Session 启动 Job；主会话给自己登记开发任务时必须为 false"
        ),
    )


class UpdateTeamTaskInput(TeamIdInput):
    task_id: str = Field(description="要更新的团队任务 ID")
    status: Literal[
        "queued",
        "in_progress",
        "blocked",
        "completed",
        "failed",
        "cancelled",
    ]
    summary: str = Field(
        default="",
        description="当前结论；blocked/completed/failed 时必须提供",
    )


def create_team_tools(
    *,
    session_id: str,
    agent_id: str,
    team_service: TeamCoordinationProtocol,
    invocation_context: ToolInvocationContext,
) -> list[BaseTool]:
    """创建默认团队协作工具组；所有权限都绑定当前 Session。"""
    current_session_id = session_id

    @tool("create_team", args_schema=CreateTeamInput)
    async def create_team(name: str) -> dict[str, object]:
        """以当前 Session 为协调者创建持久化团队任务面板。"""
        board = await team_service.create_team(
            requester_session_id=current_session_id,
            name=name,
        )
        return board.model_dump(mode="json")

    @tool("list_my_teams")
    async def list_my_teams() -> dict[str, object]:
        """列出当前 Session 已加入的团队，包括协调和手动加入的团队。"""
        result = await team_service.list_teams(requester_session_id=current_session_id)
        return result.model_dump(mode="json")

    @tool("get_team_board", args_schema=TeamIdInput)
    async def get_team_board(team_id: str) -> dict[str, object]:
        """按 team_id 读取成员 Session ID、角色、任务状态和最近团队事件。"""
        board = await team_service.get_board(
            requester_session_id=current_session_id,
            team_id=team_id,
        )
        return board.model_dump(mode="json")

    @tool("create_team_member")
    async def create_team_member(
        team_id: Annotated[str, Field(description="目标团队 ID")],
        role: Annotated[str, Field(description="成员角色，例如 reviewer 或 tester")],
        startup_prompt: Annotated[
            str,
            Field(
                description=(
                    "新成员首次启动说明，只用于确认角色和团队协议；"
                    "实际工作必须再用 assign_team_task 登记和分派"
                )
            ),
        ],
        instructions: Annotated[
            str,
            Field(description="长期角色约束，例如审查重点、测试范围和禁止修改文件"),
        ] = "",
        work_mode: Annotated[
            Literal["write", "read_only"],
            Field(description="团队职责模式；审查和测试通常用 read_only，但它不是文件沙箱"),
        ] = "read_only",
    ) -> dict[str, object]:
        """创建持久化团队成员 Session；后续由团队面板自动通知，禁止用 monitor_session_agent_end 或 collect_background_messages 等待。"""
        parent_job_id = get_current_job_id()
        if not parent_job_id:
            raise RuntimeError("create_team_member 缺少当前 job_id")
        tool_call_id = invocation_context.require_tool_call_id()
        result = await team_service.create_member(
            requester_session_id=current_session_id,
            requester_agent_id=agent_id,
            requester_job_id=parent_job_id,
            requester_tool_call_id=tool_call_id,
            team_id=team_id,
            role=role,
            startup_prompt=startup_prompt,
            instructions=instructions,
            work_mode=work_mode,
        )
        return _operation_payload(result)

    @tool("attach_team_session", args_schema=AttachTeamSessionInput)
    async def attach_team_session(
        team_id: str,
        session_id: str,
        role: str,
        instructions: str = "",
        work_mode: Literal["write", "read_only"] = "read_only",
        notify: bool = True,
    ) -> dict[str, object]:
        """把用户提供的现有 Session 加入团队；保留其历史上下文，不创建替代会话。"""
        result = await team_service.attach_session(
            requester_session_id=current_session_id,
            team_id=team_id,
            target_session_id=session_id,
            role=role,
            instructions=instructions,
            work_mode=work_mode,
            notify=notify,
        )
        return result.model_dump(mode="json")

    @tool("assign_team_task", args_schema=AssignTeamTaskInput)
    async def assign_team_task(
        team_id: str,
        assignee_session_id: str,
        title: str,
        description: str,
        phase: Literal["development", "review", "test", "fix", "other"],
        cycle: int = 1,
        depends_on_task_ids: list[str] | None = None,
        start_assignee: bool = True,
    ) -> dict[str, object]:
        """登记并启动成员任务；完成状态会自动通知协调者，分派后不要阻塞轮询或调用 monitor_session_agent_end/collect_background_messages。"""
        result = await team_service.assign_task(
            requester_session_id=current_session_id,
            team_id=team_id,
            assignee_session_id=assignee_session_id,
            title=title,
            description=description,
            phase=phase,
            cycle=cycle,
            depends_on_task_ids=list(depends_on_task_ids or []),
            start_assignee=start_assignee,
        )
        return _operation_payload(result)

    @tool("update_team_task", args_schema=UpdateTeamTaskInput)
    async def update_team_task(
        team_id: str,
        task_id: str,
        status: Literal[
            "queued",
            "in_progress",
            "blocked",
            "completed",
            "failed",
            "cancelled",
        ],
        summary: str = "",
    ) -> dict[str, object]:
        """更新当前 Session 负责的任务；重要状态会通过新 Job 通知团队协调者。"""
        result = await team_service.update_task(
            requester_session_id=current_session_id,
            team_id=team_id,
            task_id=task_id,
            status=status,
            summary=summary,
        )
        return result.model_dump(mode="json")

    return [
        create_team,
        list_my_teams,
        get_team_board,
        create_team_member,
        attach_team_session,
        assign_team_task,
        update_team_task,
    ]
