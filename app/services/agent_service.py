from __future__ import annotations

from app.schemas.agent import AgentDTO


class AgentService:
    async def list(self) -> list[AgentDTO]:
        return [
            AgentDTO(
                agent_id="planner",
                name="PlannerAgent",
                description="负责任务拆解与规划",
                model="gpt-4.1",
                tools=["workspace_search", "read_file"],
                capabilities=["planning", "routing"]
            ),
            AgentDTO(
                agent_id="executor",
                name="ExecutorAgent",
                description="负责任务执行与工具调用",
                model="gpt-4.1",
                tools=["write_file", "run_command", "edit_file"],
                capabilities=["execution", "tool_call"]
            ),
            AgentDTO(
                agent_id="reviewer",
                name="ReviewerAgent",
                description="负责代码审查与质量检查",
                model="gpt-4.1",
                tools=["read_file"],
                capabilities=["review", "analysis"]
            ),
            AgentDTO(
                agent_id="summarizer",
                name="SummarizerAgent",
                description="负责结果汇总与输出",
                model="gpt-4.1",
                tools=[],
                capabilities=["summarization", "reporting"]
            )
        ]

    async def get(self, agent_id: str) -> AgentDTO:
        agents = {a.agent_id: a for a in await self.list()}
        return agents[agent_id]
