import pytest

from app.agents.tool_invocation_context import (
    ThreadRuntimeBinding,
    ToolInvocationContext,
)
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
        return MAIN_THREAD_ID


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
        for tool in create_goal_tools(
            session_id="sess_1", goal_service=service,
            invocation_context=ToolInvocationContext(
                thread_binding=ThreadRuntimeBinding("sess_1", MAIN_THREAD_ID),
            ),
        )
    }
    assert "token 与耗时用量" in tools["get_goal"].description
    assert "必须先调用" in tools["get_goal"].description
    assert "不得从普通任务自行推断" in tools["create_goal"].description
    assert "明确要求暂停或停止" in tools["update_goal"].description
    created = await tools["create_goal"].ainvoke({"objective": "完成目标"})
    assert created["status"] == "active"
    # Goal 只属于 main thread：响应投影的 thread_id 来自 resolve_main_thread，
    # 必须与 session_id 可区分。
    assert created["thread_id"] == MAIN_THREAD_ID
    assert (await tools["get_goal"].ainvoke({}))["goal_id"] == created["goal_id"]
    paused = await tools["update_goal"].ainvoke({"status": "paused"})
    assert paused["status"] == "paused"
    with pytest.raises(ValueError, match="paused、complete 或 blocked"):
        await tools["update_goal"].ainvoke({"status": GoalStatus.active.value})
    completed = await tools["update_goal"].ainvoke({"status": "complete"})
    assert completed["status"] == "complete"


@pytest.mark.parametrize("name,payload", [
    ("get_goal", {}),
    ("create_goal", {"objective": "不得污染主线程"}),
    ("update_goal", {"status": "complete"}),
])
async def test_child_goal_tools_reject_without_modifying_main_goal(name, payload):
    store = _Store()
    service = SessionGoalService(store=store, session_service=_Sessions(), job_event_bus=_Bus())
    tools = {tool.name: tool for tool in create_goal_tools(
        session_id="sess_1", goal_service=service,
        invocation_context=ToolInvocationContext(
            thread_binding=ThreadRuntimeBinding("sess_1", CHILD_THREAD_ID),
        ),
    )}
    with pytest.raises(ValueError, match="child thread"):
        await tools[name].ainvoke(payload)
    assert store.goal is None
MAIN_THREAD_ID = "thr_019fca64740070008000000000000001"
CHILD_THREAD_ID = "thr_019fca64740070008000000000000002"
