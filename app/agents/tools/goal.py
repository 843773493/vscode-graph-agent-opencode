from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from app.agents.tool_invocation_context import ToolInvocationContext
from app.schemas.internal_v2.goal import GoalStatus
from app.services.business.session_goal_service import SessionGoalService


def create_goal_tools(
    *, session_id: str, goal_service: SessionGoalService, invocation_context: ToolInvocationContext
) -> list[BaseTool]:
    async def assert_main_thread() -> None:
        binding = invocation_context.thread_binding
        if binding is None or binding.session_id != session_id:
            raise RuntimeError("Goal 工具缺少精确 ThreadRuntimeBinding")
        await goal_service.assert_agent_thread(session_id, binding.thread_id)

    @tool
    async def get_goal() -> dict[str, object] | None:
        """读取当前会话的持久化 Goal，包括状态、预算、token 与耗时用量。

        用户询问、要求停止或控制 Goal 时必须先调用；只有本工具返回 None 时，
        才能说当前会话没有 Goal。
        """
        await assert_main_thread()
        goal = await goal_service.get(session_id)
        return None if goal is None else goal.model_dump(mode="json")

    @tool
    async def create_goal(
        objective: str, token_budget: int | None = None
    ) -> dict[str, object]:
        """仅当用户或 system/developer 指令明确要求时创建 Goal，不得从普通任务自行推断。只有明确要求 token 预算时才传 token_budget；存在未完成 Goal 时调用失败。"""
        await assert_main_thread()
        goal = await goal_service.create_for_agent(session_id, objective, token_budget)
        return goal.model_dump(mode="json")

    @tool
    async def update_goal(status: str) -> dict[str, object]:
        """用户明确要求暂停或停止当前 Goal 时可将状态设为 paused；先调用 get_goal。

        只有目标实际完成且没有遗留工作时才能标记 complete。只有相同阻塞连续
        至少三个 Goal 轮次出现、且没有用户输入或外部状态变化就无法取得有意义
        进展时才能标记 blocked；恢复已阻塞 Goal 后应重新计算轮次。困难、缓慢、
        不确定、尚未完成或希望澄清都不构成 blocked。不得仅因预算即将耗尽或
        准备停止工作而标记 complete。不得自行恢复或清除 Goal。
        """
        try:
            parsed = GoalStatus(status)
        except ValueError as exc:
            raise ValueError("status 只能是 paused、complete 或 blocked") from exc
        await assert_main_thread()
        goal = await goal_service.update_for_agent(session_id, parsed)
        return goal.model_dump(mode="json")

    return [get_goal, create_goal, update_goal]
