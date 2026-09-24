import pytest

from app.agents.tools.goal import create_goal_tools
from app.schemas.internal_v2.goal import GoalStatus
from app.services.business.session_goal_service import SessionGoalService


class _Sessions:
    async def get(self, session_id):
        return session_id

    async def resolve_main_thread(self, session_id):
        # 生产实现取自 catalog 冻结的 main_thread_id，绝不用 session_id 冒充
        # thread_id；替身必须返回可区分的固定 thread 身份，否则「误用
        # session_id」的变异无法被断言杀掉。
        return f"thr_{session_id}"


class _Store:
    goal = None

    def read(self, session_id):
        return self.goal

    def write(self, goal):
        self.goal = goal

    def clear(self, session_id):
        self.goal = None
        return True

    def list_existing(self):
        return [] if self.goal is None else [self.goal]


class _Bus:
    async def publish(self, **event):
        return event


@pytest.mark.asyncio
async def test_goal_agent_tools_enforce_status_boundary():
    service = SessionGoalService(
        store=_Store(), session_service=_Sessions(), job_event_bus=_Bus()
    )
    tools = {
        tool.name: tool
        for tool in create_goal_tools(session_id="sess_1", goal_service=service)
    }
    assert "token 与耗时用量" in tools["get_goal"].description
    assert "不得从普通任务自行推断" in tools["create_goal"].description
    assert "预算限制由用户或系统控制" in tools["update_goal"].description
    created = await tools["create_goal"].ainvoke({"objective": "完成目标"})
    assert created["status"] == "active"
    # Goal 只属于 main thread：响应投影的 thread_id 来自 resolve_main_thread，
    # 必须与 session_id 可区分。
    assert created["thread_id"] == "thr_sess_1"
    assert (await tools["get_goal"].ainvoke({}))["goal_id"] == created["goal_id"]
    with pytest.raises(ValueError, match="complete 或 blocked"):
        await tools["update_goal"].ainvoke({"status": GoalStatus.paused.value})
    completed = await tools["update_goal"].ainvoke({"status": "complete"})
    assert completed["status"] == "complete"
