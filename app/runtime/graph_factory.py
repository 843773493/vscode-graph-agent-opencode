"""GraphFactory 的 SDK 懒加载边界；selector 注册不依赖执行模块导入。"""

from __future__ import annotations

from typing import Any


def build_deep_agent_graph(**kwargs: Any) -> object:
    """按注册的精确 selector 构建图，原样传递 thread generation 依赖。"""
    from app.agents.agent_factory import create_my_deep_agent

    return create_my_deep_agent(**kwargs)
