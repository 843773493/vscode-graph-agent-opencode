"""GraphFactory 的 SDK 懒加载边界；selector 注册不依赖执行模块导入。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.orchestration.event_stream.contracts import AgentEventSource


def assemble_deep_agent(**kwargs: Any) -> AgentEventSource:
    """懒加载单一装配实现，原样传递 thread generation 执行依赖。"""
    from app.agents.agent_factory import assemble_deep_agent

    return assemble_deep_agent(**kwargs)
