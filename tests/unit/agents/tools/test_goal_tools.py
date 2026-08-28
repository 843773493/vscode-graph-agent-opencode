import pytest

from app.agents.tools.goal import create_goal_tools
from app.schemas.internal_v2.goal import GoalStatus
from app.services.business.session_goal_service import SessionGoalService


class _Sessions:
    async def get(self, session_id):
        return session_id


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
    assert (await tools["get_goal"].ainvoke({}))["goal_id"] == created["goal_id"]
    with pytest.raises(ValueError, match="complete 或 blocked"):
        await tools["update_goal"].ainvoke({"status": GoalStatus.paused.value})
    completed = await tools["update_goal"].ainvoke({"status": "complete"})
    assert completed["status"] == "complete"
